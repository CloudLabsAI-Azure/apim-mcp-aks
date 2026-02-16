#!/usr/bin/env python3
"""Verify the fine-tuned model is active on the server."""
import requests, json

base_url = 'http://localhost:8000/runtime/webhooks/mcp'
r = requests.get(f'{base_url}/sse', stream=True, timeout=10)
for line in r.iter_lines(decode_unicode=True):
    if line and line.startswith('data: '):
        url = f'{base_url}/{line[6:].strip()}'
        r.close()
        result = requests.post(url, json={
            'jsonrpc': '2.0', 'id': 1,
            'method': 'tools/call',
            'params': {'name': 'get_evaluation_status', 'arguments': {}}
        }, timeout=30)
        data = result.json()
        content = data.get('result', {}).get('content', [])
        if content:
            text = content[0].get('text', '{}')
            parsed = json.loads(text)
            model = parsed.get("model_deployment", "?")
            tuned = parsed.get("use_tuned_model", "?")
            tuned_name = parsed.get("tuned_model_name", "?")
            print(f"Model: {model}")
            print(f"Use Tuned: {tuned}")
            print(f"Tuned Name: {tuned_name}")
        break
