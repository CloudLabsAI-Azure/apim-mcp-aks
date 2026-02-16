#!/usr/bin/env python3
"""
End-to-end fine-tuning pipeline: generate episodes, label, build dataset, train.
Sends MHP domain queries to the MCP agent, then orchestrates Lightning fine-tuning.
"""

import json
import re
import sys
import time
import requests
import argparse


def get_session_url(base_url: str) -> str:
    """Establish SSE session and return the message URL."""
    resp = requests.get(f"{base_url}/sse", stream=True, timeout=15)
    for line in resp.iter_lines(decode_unicode=True):
        if line and line.startswith("data: "):
            msg_path = line[6:].strip()
            resp.close()
            return f"{base_url}/{msg_path}"
    raise RuntimeError("Failed to obtain SSE session URL")


def mcp_call(base_url: str, tool_name: str, arguments: dict, timeout: int = 120) -> dict:
    """Call an MCP tool and return the parsed result."""
    session_url = get_session_url(base_url)
    resp = requests.post(
        session_url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
        timeout=timeout,
    )
    result = resp.json()
    content = result.get("result", {}).get("content", [])
    if not content:
        return {"error": f"Empty response: {result}"}
    text = content[0].get("text", "{}")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


def main():
    parser = argparse.ArgumentParser(description="End-to-end fine-tuning pipeline")
    parser.add_argument("--port", type=int, default=8000, help="MCP port (default: 8000)")
    parser.add_argument("--agent-id", default="mcp-agents", help="Agent ID")
    parser.add_argument("--skip-episodes", action="store_true", help="Skip episode generation")
    parser.add_argument("--skip-label", action="store_true", help="Skip labeling")
    parser.add_argument("--skip-dataset", action="store_true", help="Skip dataset build")
    parser.add_argument("--skip-training", action="store_true", help="Skip training start")
    parser.add_argument("--check-status", type=str, help="Just check training run status")
    args = parser.parse_args()

    base_url = f"http://localhost:{args.port}/runtime/webhooks/mcp"

    # If just checking status
    if args.check_status:
        print(f"Checking training run {args.check_status}...")
        result = mcp_call(base_url, "lightning_get_training_status", {
            "training_run_id": args.check_status,
            "agent_id": args.agent_id,
        })
        print(json.dumps(result, indent=2))
        return 0

    # Load MHP eval queries
    eval_file = "evals/healthcare_digital_quality/healthcare_digital_quality_eval_data.jsonl"
    queries = []
    with open(eval_file) as f:
        for line in f:
            line = line.strip()
            if line:
                obj = json.loads(line)
                queries.append(obj)

    print(f"Loaded {len(queries)} MHP domain queries")
    print("=" * 70)

    # ── Step 1: Generate Episodes ──
    if not args.skip_episodes:
        print("\n▶ STEP 1: Generating episodes from MHP domain queries...")
        success = 0
        for i, item in enumerate(queries, 1):
            query = item["query"]
            print(f"  [{i}/{len(queries)}] {query[:70]}...")
            try:
                result = mcp_call(base_url, "next_best_action", {
                    "task": query
                }, timeout=180)
                if "error" not in result:
                    print(f"    ✓ Episode captured")
                    success += 1
                else:
                    print(f"    ✗ Error: {str(result.get('error', ''))[:80]}")
            except Exception as e:
                print(f"    ✗ Exception: {e}")
            time.sleep(3)  # cooldown between calls
        print(f"\n  Episodes generated: {success}/{len(queries)}")
    else:
        print("\n▶ STEP 1: Skipping episode generation (--skip-episodes)")

    # ── Step 2: List and Label Episodes ──
    if not args.skip_label:
        print("\n▶ STEP 2: Listing and labeling episodes...")
        episodes_data = mcp_call(base_url, "lightning_list_episodes", {
            "agent_id": args.agent_id,
            "limit": 50
        })

        if "error" in episodes_data:
            print(f"  Error listing episodes: {episodes_data['error']}")
            return 1

        episodes = episodes_data.get("episodes", [])
        print(f"  Found {len(episodes)} episodes")

        labeled = 0
        for i, ep in enumerate(episodes, 1):
            ep_id = ep.get("id", "")
            if not ep_id:
                continue

            # Score based on output quality
            output = ep.get("assistant_output", "")
            tool_calls_count = ep.get("tool_calls_count", 0)
            tool_names = ep.get("tool_names", [])

            # MHP-specific scoring: check for MHP protocol keywords
            mhp_keywords = ["MHP", "MHP-QP", "Meridian", "HEDIS", "quality score",
                           "risk tier", "Tier 1", "Tier 2", "measure weight",
                           "outreach cadence", "provider performance", "engagement score",
                           "priority score", "cost-effectiveness"]

            output_lower = output.lower() if output else ""
            mhp_matches = sum(1 for kw in mhp_keywords if kw.lower() in output_lower)

            has_error = any(w in output_lower for w in ["error", "failed", "exception", "traceback"])

            if has_error:
                score = 0.3
                reason = "Output contains errors"
            elif mhp_matches >= 5:
                score = 0.95
                reason = f"Excellent MHP groundedness ({mhp_matches} protocol refs)"
            elif mhp_matches >= 3:
                score = 0.85
                reason = f"Good MHP groundedness ({mhp_matches} protocol refs)"
            elif mhp_matches >= 1:
                score = 0.7
                reason = f"Some MHP references ({mhp_matches} protocol refs)"
            elif tool_calls_count >= 1 and len(output) > 100:
                score = 0.6
                reason = "Complete response but no MHP protocol references"
            else:
                score = 0.5
                reason = "Generic response"

            result = mcp_call(base_url, "lightning_assign_reward", {
                "episode_id": ep_id,
                "reward_value": score,
                "reward_source": "eval_score",
                "agent_id": args.agent_id,
                "rubric": "mhp_groundedness",
                "evaluator": "mhp_auto_labeler",
                "comments": reason,
            })

            status = "✓" if result.get("success") else "✗"
            print(f"  [{i}] {status} score={score:.2f} - {reason[:50]}")
            if result.get("success"):
                labeled += 1

        print(f"\n  Labeled: {labeled}/{len(episodes)}")
    else:
        print("\n▶ STEP 2: Skipping labeling (--skip-label)")

    # ── Step 3: Build Dataset ──
    if not args.skip_dataset:
        print("\n▶ STEP 3: Building training dataset...")
        dataset_result = mcp_call(base_url, "lightning_build_dataset", {
            "agent_id": args.agent_id,
            "min_reward": 0.5,
        })

        if "error" in dataset_result:
            print(f"  Error: {dataset_result['error']}")
            return 1

        dataset_id = dataset_result.get("dataset_id", "unknown")
        examples = dataset_result.get("examples_count", 0)
        print(f"  Dataset ID: {dataset_id}")
        print(f"  Training examples: {examples}")

        if examples < 10:
            print(f"  WARNING: Only {examples} examples. Azure OpenAI requires minimum 10.")
    else:
        print("\n▶ STEP 3: Skipping dataset build (--skip-dataset)")

    # ── Step 4: Start Training ──
    if not args.skip_training:
        print("\n▶ STEP 4: Starting fine-tuning training run...")
        training_result = mcp_call(base_url, "lightning_start_training", {
            "agent_id": args.agent_id,
        }, timeout=180)

        if "error" in training_result:
            print(f"  Error: {json.dumps(training_result, indent=2)}")
            return 1

        training_id = training_result.get("training_run_id", "unknown")
        status = training_result.get("status", "unknown")
        print(f"  Training Run ID: {training_id}")
        print(f"  Status: {status}")
        print(f"\n  Monitor with:")
        print(f"    python scripts/run_finetuning.py --check-status {training_id}")
        print(f"    python scripts/monitor_training.py --training-run-id {training_id}")
    else:
        print("\n▶ STEP 4: Skipping training start (--skip-training)")

    print("\n" + "=" * 70)
    print("Pipeline complete!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
