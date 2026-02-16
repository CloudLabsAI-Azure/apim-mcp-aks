#!/usr/bin/env python3
"""Send healthcare eval queries to mcp-agents to generate episodes for training data."""

import asyncio
import aiohttp
import json
import re
import sys
import time


async def main():
    port = 8000
    base_url = f"http://localhost:{port}/runtime/webhooks/mcp"
    data_file = "evals/healthcare_digital_quality/healthcare_digital_quality_eval_data.jsonl"

    # Load eval queries and tool calls
    items = []
    with open(data_file) as f:
        for line in f:
            line = line.strip()
            if line:
                obj = json.loads(line)
                items.append(obj)

    print(f"Loaded {len(items)} queries from {data_file}")

    async with aiohttp.ClientSession() as session:
        # Establish SSE session
        print("Establishing SSE session...")
        async with session.get(
            f"{base_url}/sse", headers={"Accept": "text/event-stream"}
        ) as sse:
            session_url = None
            async for chunk in sse.content.iter_chunked(1024):
                data = chunk.decode("utf-8", errors="ignore")
                match = re.search(r"data: (message\?[^\n\r]+)", data)
                if match:
                    session_url = f"{base_url}/{match.group(1)}"
                    break

            if not session_url:
                print("ERROR: No session URL obtained")
                return 1

            print(f"Session established: {session_url}")

            success_count = 0
            for i, item in enumerate(items, 1):
                query = item["query"]
                # Use the tool_calls from the eval data
                tc = item.get("tool_calls", [{}])[0] if item.get("tool_calls") else {}
                tool_name = tc.get("name", "next_best_action")
                tool_args = tc.get("arguments", {"task": query})

                print(f"\n[{i}/{len(items)}] Tool: {tool_name} | Intent: {item.get('ground_truth_intent', 'unknown')}")
                print(f"  Query: {query[:80]}...")

                request = {
                    "jsonrpc": "2.0",
                    "id": f"healthcare-episode-{i}",
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": tool_args,
                    },
                }

                try:
                    async with session.post(
                        session_url,
                        json=request,
                        timeout=aiohttp.ClientTimeout(total=120),
                    ) as resp:
                        text = await resp.text()
                        if resp.status == 200:
                            result = json.loads(text)
                            if "error" not in result:
                                content = result.get("result", {}).get("content", [])
                                if content:
                                    preview = content[0].get("text", "")[:120]
                                    print(f"  OK - {preview}...")
                                else:
                                    print(f"  OK (no content)")
                                success_count += 1
                            else:
                                err = str(result.get("error", ""))[:100]
                                print(f"  Error in response: {err}")
                        else:
                            print(f"  HTTP {resp.status}: {text[:100]}")
                except asyncio.TimeoutError:
                    print(f"  Timeout (120s)")
                except Exception as e:
                    print(f"  Error: {e}")

                # Pause between requests to avoid SSE issues
                if i < len(items):
                    print(f"  Waiting 3s...")
                    await asyncio.sleep(3)

    print(f"\nDone! {success_count}/{len(items)} queries generated episodes successfully.")
    return 0


async def list_tools():
    """List available tools on the agent."""
    port = 8000
    base_url = f"http://localhost:{port}/runtime/webhooks/mcp"
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{base_url}/sse", headers={"Accept": "text/event-stream"}
        ) as sse:
            session_url = None
            async for chunk in sse.content.iter_chunked(1024):
                data = chunk.decode("utf-8", errors="ignore")
                match = re.search(r"data: (message\?[^\n\r]+)", data)
                if match:
                    session_url = f"{base_url}/{match.group(1)}"
                    break
            if session_url:
                req = {"jsonrpc": "2.0", "id": "list-tools", "method": "tools/list"}
                async with session.post(
                    session_url, json=req, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    result = json.loads(await resp.text())
                    tools = result.get("result", {}).get("tools", [])
                    print(f"Available tools ({len(tools)}):")
                    for t in tools:
                        name = t["name"]
                        desc = t.get("description", "")[:80]
                        print(f"  {name}: {desc}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--list-tools":
        asyncio.run(list_tools())
    else:
        sys.exit(asyncio.run(main()))
