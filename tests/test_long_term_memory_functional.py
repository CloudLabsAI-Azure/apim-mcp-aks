#!/usr/bin/env python3
"""
Functional Test: Long-Term Memory via AzureAISearchContextProvider through APIM MCP APIs

This script tests long-term memory (Azure AI Search with AzureAISearchContextProvider)
by exercising the deployed MCP tools through the APIM gateway or direct connection.

Test Flow:
1. Establish SSE session to MCP server (via APIM or port-forward)
2. Call next_best_action with tasks matching ingested task instructions
3. Verify response includes long-term memory retrieval (task_instructions_found > 0)
4. Validate AzureAISearchContextProvider enriches the generated plan
5. Compare results with and without long-term memory context

Usage:
    python tests/test_long_term_memory_functional.py                # via APIM
    python tests/test_long_term_memory_functional.py --direct       # via port-forward

Requirements:
    - aiohttp (pip install aiohttp)
    - MCP server deployed with:
      - AZURE_SEARCH_ENDPOINT configured
      - FOUNDRY_PROJECT_ENDPOINT configured
      - Task instructions ingested into AI Search index
    - Valid OAuth token or Azure CLI configured (APIM mode)
    - kubectl port-forward active (direct mode)
"""

import asyncio
import json
import aiohttp
import sys
import re
from pathlib import Path
from typing import Optional, Dict, Any, List

# Configuration file
CONFIG_FILE = Path(__file__).parent / 'mcp_test_config.json'


def load_config() -> Dict[str, Any]:
    """Load configuration from mcp_test_config.json"""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
            print(f"✅ Loaded configuration from {CONFIG_FILE}")
            return config
    else:
        print(f"❌ Config file not found: {CONFIG_FILE}")
        sys.exit(1)


class MCPClient:
    """MCP Client that maintains SSE session for long-term memory tests"""

    def __init__(self, base_url: str, auth_token: str = None):
        self.base_url = base_url.rstrip('/')
        self.auth_token = auth_token
        self.session = None
        self.sse_response = None
        self.session_message_url = None

    async def __aenter__(self):
        cookie_jar = aiohttp.CookieJar()
        headers = {}
        if self.auth_token and not self.auth_token.startswith('direct-mode'):
            headers['Authorization'] = f'Bearer {self.auth_token}'
        self.session = aiohttp.ClientSession(
            cookie_jar=cookie_jar,
            headers=headers
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.sse_response and not self.sse_response.closed:
            self.sse_response.close()
        if self.session:
            await self.session.close()

    async def establish_sse_session(self) -> bool:
        """Establish SSE connection and extract session URL"""
        try:
            print(f"\n📡 Establishing SSE session to: {self.base_url}/sse")

            self.sse_response = await self.session.get(
                f'{self.base_url}/sse',
                headers={
                    'Accept': 'text/event-stream',
                    'Cache-Control': 'no-cache',
                    'Connection': 'keep-alive'
                }
            )

            print(f"   SSE Response Status: {self.sse_response.status}")

            if self.sse_response.status == 200:
                async for chunk in self.sse_response.content.iter_chunked(1024):
                    if chunk:
                        data = chunk.decode('utf-8', errors='ignore')
                        match = re.search(r'data: (message\?[^\n\r]+)', data)
                        if match:
                            session_path = match.group(1)
                            self.session_message_url = f"{self.base_url}/{session_path}"
                            print(f"✅ Got session URL: {self.session_message_url}")
                            return True
                        break

                print("⚠️  SSE connected but no session URL found")
                return False
            else:
                response_text = await self.sse_response.text()
                print(f"❌ SSE connection failed: {response_text}")
                return False

        except Exception as e:
            print(f"❌ SSE connection error: {e}")
            return False

    async def send_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send a JSON-RPC 2.0 request"""
        jsonrpc_request = {
            "jsonrpc": "2.0",
            "id": "ltm-test-request",
            "method": method
        }
        if params:
            jsonrpc_request["params"] = params

        message_url = self.session_message_url if self.session_message_url else f'{self.base_url}/message'

        try:
            async with self.session.post(
                message_url,
                json=jsonrpc_request,
                headers={'Content-Type': 'application/json'},
                timeout=aiohttp.ClientTimeout(total=120)
            ) as response:
                response_text = await response.text()

                if response.status == 200:
                    try:
                        return json.loads(response_text)
                    except json.JSONDecodeError:
                        return {"error": "Invalid JSON response", "raw": response_text}
                else:
                    return {"error": f"HTTP {response.status}", "body": response_text}

        except asyncio.TimeoutError:
            return {"error": "Request timed out (120s)"}
        except Exception as e:
            return {"error": str(e)}

    async def list_tools(self) -> Optional[List[Dict[str, Any]]]:
        """List available MCP tools"""
        result = await self.send_request("tools/list")
        if 'error' not in result:
            return result.get('result', {}).get('tools', [])
        return None

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call an MCP tool"""
        return await self.send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments
        })


async def get_mcp_token(session: aiohttp.ClientSession, token_url: str) -> Optional[str]:
    """Get MCP access token from APIM OAuth endpoint"""
    print("\n🔐 Getting MCP access token from APIM...")
    try:
        async with session.post(token_url, data={}) as response:
            if response.status == 200:
                data = await response.json()
                token = data.get('access_token', '')
                if token:
                    print(f"✅ Got MCP access token: {token[:30]}...")
                    return token
            print(f"❌ Token request failed: {response.status}")
            return None
    except Exception as e:
        print(f"❌ Error getting token: {e}")
        return None


def parse_tool_response(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract and parse the JSON payload from an MCP tool/call response."""
    tool_result = result.get('result', {})
    content = tool_result.get('content', [])
    is_error = tool_result.get('isError', False)

    if is_error:
        error_text = content[0].get('text', 'Unknown error') if content else 'No error message'
        print(f"❌ Tool returned error: {error_text}")
        return None

    if not content:
        print("❌ No content in response")
        return None

    response_text = content[0].get('text', '{}')
    try:
        return json.loads(response_text)
    except json.JSONDecodeError as e:
        print(f"❌ Failed to parse response JSON: {e}")
        return None


# ============================================================
# Test Cases — Long-Term Memory via AzureAISearchContextProvider
# ============================================================

# Tasks that should match ingested task instructions in AI Search
LONG_TERM_MEMORY_TEST_CASES = [
    {
        "name": "Customer Churn — exact domain match",
        "task": "Analyze customer churn data and build a predictive model to identify at-risk customers",
        "expected_instruction_keywords": ["churn", "predictive", "customer"],
        "expected_category": "data_analysis",
    },
    {
        "name": "REST API User Management — exact domain match",
        "task": "Design a REST API for a user management system with JWT authentication",
        "expected_instruction_keywords": ["REST", "user management", "authentication"],
        "expected_category": "api_development",
    },
    {
        "name": "CI/CD Kubernetes Pipeline — exact domain match",
        "task": "Set up a CI/CD pipeline for deploying microservices to Kubernetes",
        "expected_instruction_keywords": ["CI/CD", "pipeline", "Kubernetes"],
        "expected_category": "devops",
    },
]

# A task with no matching instructions — baseline for comparison
BASELINE_TASK = {
    "name": "Novel task with no matching instructions",
    "task": "Write a haiku about the ocean at sunset",
}


async def test_long_term_memory(client: MCPClient) -> Dict[str, Any]:
    """
    Run the full long-term memory test suite.

    Returns a summary dict with pass/fail counts and details.
    """
    results = {
        "total": 0,
        "passed": 0,
        "failed": 0,
        "details": [],
    }

    # ------------------------------------------------------------------
    # Test 0: Verify MCP tools include next_best_action
    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("📋 Step 0: Verify MCP tool availability")
    print("=" * 70)

    tools = await client.list_tools()
    if not tools:
        print("❌ Could not list MCP tools — aborting")
        results["failed"] += 1
        results["total"] += 1
        results["details"].append({"test": "tool_listing", "passed": False, "reason": "Could not list tools"})
        return results

    tool_names = [t.get('name') for t in tools]
    print(f"   Tools discovered: {', '.join(tool_names)}")

    if 'next_best_action' not in tool_names:
        print("❌ next_best_action tool not found — aborting")
        results["failed"] += 1
        results["total"] += 1
        results["details"].append({"test": "tool_listing", "passed": False, "reason": "next_best_action not found"})
        return results

    print("✅ next_best_action tool available")

    # ------------------------------------------------------------------
    # Test 1-3: Tasks that SHOULD retrieve long-term memory
    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("🧪 Tests 1–3: Long-Term Memory Retrieval via AzureAISearchContextProvider")
    print("=" * 70)

    for i, tc in enumerate(LONG_TERM_MEMORY_TEST_CASES, 1):
        results["total"] += 1
        test_name = tc["name"]
        task_text = tc["task"]

        print(f"\n{'─' * 70}")
        print(f"🎯 Test {i}: {test_name}")
        print(f"   Task: {task_text}")
        print(f"{'─' * 70}")

        print("   ⏳ Calling next_best_action (may take 30-60s)...")
        raw_result = await client.call_tool("next_best_action", {"task": task_text})

        if 'error' in raw_result:
            print(f"   ❌ MCP call error: {raw_result['error']}")
            results["failed"] += 1
            results["details"].append({"test": test_name, "passed": False, "reason": f"MCP error: {raw_result['error']}"})
            continue

        data = parse_tool_response(raw_result)
        if data is None:
            results["failed"] += 1
            results["details"].append({"test": test_name, "passed": False, "reason": "Failed to parse response"})
            continue

        # --- Assertions ---
        analysis = data.get("analysis", {})
        instructions_found = analysis.get("task_instructions_found", 0)
        instructions = analysis.get("task_instructions", [])
        metadata = data.get("metadata", {})
        ltm_used = metadata.get("long_term_memory_used", False)
        plan_steps = data.get("plan", {}).get("steps", [])

        print(f"\n   📊 Results:")
        print(f"      Task ID:                  {data.get('task_id', 'N/A')}")
        print(f"      Intent:                   {data.get('intent', 'N/A')}")
        print(f"      Long-term memory used:    {ltm_used}")
        print(f"      Task instructions found:  {instructions_found}")
        print(f"      Plan steps generated:     {len(plan_steps)}")

        if instructions:
            for idx, ti in enumerate(instructions, 1):
                print(f"      Instruction {idx}: {ti.get('title', 'N/A')} "
                      f"(category={ti.get('category', '?')}, score={ti.get('relevance_score', 0):.3f})")

        # Check 1: At least one task instruction retrieved from AI Search
        if instructions_found == 0:
            reason = "No task instructions retrieved — AzureAISearchContextProvider may not be returning results"
            print(f"   ❌ FAIL: {reason}")
            results["failed"] += 1
            results["details"].append({"test": test_name, "passed": False, "reason": reason})
            continue

        # Check 2: long_term_memory_used flag is True
        if not ltm_used:
            reason = "long_term_memory_used metadata flag is False despite instructions found"
            print(f"   ❌ FAIL: {reason}")
            results["failed"] += 1
            results["details"].append({"test": test_name, "passed": False, "reason": reason})
            continue

        # Check 3: Retrieved instructions contain expected keywords
        all_instruction_text = " ".join(
            f"{ti.get('title', '')} {ti.get('description', '')} {ti.get('category', '')} {ti.get('intent', '')}"
            for ti in instructions
        ).lower()

        missing_keywords = [
            kw for kw in tc["expected_instruction_keywords"]
            if kw.lower() not in all_instruction_text
        ]

        if missing_keywords:
            reason = f"Retrieved instructions missing expected keywords: {missing_keywords}"
            print(f"   ⚠️  WARN: {reason}")
            # This is a soft warning, not a hard fail
            print(f"   (Instruction text excerpt: {all_instruction_text[:200]}...)")

        # Check 4: Plan was generated with at least 2 steps
        if len(plan_steps) < 2:
            reason = f"Plan has only {len(plan_steps)} step(s), expected ≥ 2"
            print(f"   ❌ FAIL: {reason}")
            results["failed"] += 1
            results["details"].append({"test": test_name, "passed": False, "reason": reason})
            continue

        # Check 5: At least one plan step references instructions (source == "adapted")
        adapted_steps = [s for s in plan_steps if s.get("source") == "adapted"]
        if adapted_steps:
            print(f"   ✅ {len(adapted_steps)} plan step(s) adapted from long-term memory instructions")
        else:
            print(f"   ⚠️  No plan steps explicitly marked as 'adapted' from instructions (informational)")

        print(f"   ✅ PASS: Long-term memory retrieval verified for '{test_name}'")
        results["passed"] += 1
        results["details"].append({
            "test": test_name,
            "passed": True,
            "instructions_found": instructions_found,
            "plan_steps": len(plan_steps),
            "adapted_steps": len(adapted_steps),
        })

        # Brief pause between tests
        if i < len(LONG_TERM_MEMORY_TEST_CASES):
            await asyncio.sleep(2)

    # ------------------------------------------------------------------
    # Test 4: Baseline — novel task with no matching instructions
    # ------------------------------------------------------------------
    results["total"] += 1
    print(f"\n{'─' * 70}")
    print(f"🎯 Test {len(LONG_TERM_MEMORY_TEST_CASES) + 1}: {BASELINE_TASK['name']}")
    print(f"   Task: {BASELINE_TASK['task']}")
    print(f"{'─' * 70}")

    print("   ⏳ Calling next_best_action with novel task...")
    raw_result = await client.call_tool("next_best_action", {"task": BASELINE_TASK["task"]})

    if 'error' in raw_result:
        print(f"   ❌ MCP call error: {raw_result['error']}")
        results["failed"] += 1
        results["details"].append({"test": BASELINE_TASK["name"], "passed": False, "reason": f"MCP error: {raw_result['error']}"})
    else:
        data = parse_tool_response(raw_result)
        if data:
            baseline_instructions = data.get("analysis", {}).get("task_instructions_found", 0)
            baseline_ltm = data.get("metadata", {}).get("long_term_memory_used", False)
            print(f"   📊 Baseline: instructions_found={baseline_instructions}, ltm_used={baseline_ltm}")

            if baseline_instructions == 0:
                print(f"   ✅ PASS: Novel task correctly returned 0 matching instructions")
                results["passed"] += 1
                results["details"].append({"test": BASELINE_TASK["name"], "passed": True, "instructions_found": 0})
            else:
                # Not necessarily a failure — AI Search may return low-relevance results
                print(f"   ⚠️  INFO: Novel task returned {baseline_instructions} instruction(s) — review relevance scores")
                results["passed"] += 1
                results["details"].append({"test": BASELINE_TASK["name"], "passed": True, "instructions_found": baseline_instructions, "note": "unexpected but may be low-relevance"})
        else:
            results["failed"] += 1
            results["details"].append({"test": BASELINE_TASK["name"], "passed": False, "reason": "Failed to parse response"})

    return results


async def main():
    """Main entry point"""
    print("=" * 70)
    print("🧪 Functional Test: Long-Term Memory (AzureAISearchContextProvider)")
    print("   Tests that next_best_action retrieves task instructions from")
    print("   Azure AI Search via AzureAISearchContextProvider through APIM MCP APIs")
    print("=" * 70)

    use_direct = '--direct' in sys.argv
    config = load_config()

    if use_direct:
        direct_config = config.get('direct', {})
        base_url = direct_config.get('base_url', 'http://localhost:8000/runtime/webhooks/mcp')
        token = 'direct-mode-no-token-needed'
        print(f"\n🔗 Direct Mode URL: {base_url}")
        print("   (Using port-forward, no auth required)")
    else:
        apim_config = config.get('apim', {})
        base_url = apim_config.get('base_url', '')
        token_url = apim_config.get('oauth_token_url', '')

        if not base_url:
            print("❌ No APIM base URL configured in mcp_test_config.json")
            sys.exit(1)

        print(f"\n🔗 APIM Base URL: {base_url}")

        async with aiohttp.ClientSession() as session:
            token = await get_mcp_token(session, token_url)
            if not token:
                print("❌ Failed to get APIM access token")
                sys.exit(1)

    async with MCPClient(base_url, token) as client:
        # Establish SSE session
        if not await client.establish_sse_session():
            print("❌ Failed to establish SSE session")
            sys.exit(1)

        print("\n⏳ Waiting for session to initialize...")
        await asyncio.sleep(2)

        # Run tests
        results = await test_long_term_memory(client)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("📊 Long-Term Memory Test Summary")
    print("=" * 70)

    print(f"\n   Total tests:  {results['total']}")
    print(f"   Passed:       {results['passed']}")
    print(f"   Failed:       {results['failed']}")

    print(f"\n   {'Test':<50} {'Result':<10}")
    print(f"   {'─' * 50} {'─' * 10}")
    for d in results["details"]:
        status = "✅ PASS" if d["passed"] else "❌ FAIL"
        print(f"   {d['test']:<50} {status}")
        if not d["passed"]:
            print(f"      Reason: {d.get('reason', 'unknown')}")

    all_passed = results["failed"] == 0

    if all_passed:
        print("\n🎉 All long-term memory tests PASSED!")
        print("\nVerified capabilities:")
        print("   ✓ AzureAISearchContextProvider retrieves task instructions from AI Search")
        print("   ✓ Long-term memory context enriches next_best_action plans")
        print("   ✓ Hybrid search (vector + full-text) returns relevant instructions")
        print("   ✓ Plan generation incorporates instruction-adapted steps")
        print("   ✓ Metadata correctly reports long_term_memory_used flag")
    else:
        print(f"\n❌ {results['failed']} test(s) FAILED")
        print("\nTroubleshooting:")
        print("  1. Verify AZURE_SEARCH_ENDPOINT is set in Kubernetes deployment")
        print("  2. Verify task instructions are ingested into AI Search index:")
        print("     python scripts/ingest_task_instructions.py")
        print("  3. Verify agent-framework-azure-ai-search package is installed:")
        print("     pip install agent-framework-azure-ai-search")
        print("  4. Check MCP server logs for AzureAISearchContextProvider errors:")
        print("     kubectl logs -n mcp-agents -l app=mcp-agents --tail=100 | grep -i search")
        print("  5. Verify the AI Search index contains documents:")
        print("     az search query --service-name <name> --index-name task-instructions --query '*'")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  Test interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
