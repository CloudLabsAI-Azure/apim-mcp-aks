#!/usr/bin/env python3
"""Build dataset and start training."""
import requests, json, sys

base_url = 'http://localhost:8000/runtime/webhooks/mcp'

def get_session():
    r = requests.get(f'{base_url}/sse', stream=True, timeout=15)
    for line in r.iter_lines(decode_unicode=True):
        if line and line.startswith('data: '):
            r.close()
            return f'{base_url}/{line[6:].strip()}'

def call_tool(name, args):
    url = get_session()
    r = requests.post(url, json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}}, timeout=180)
    result = r.json()
    content = result.get('result',{}).get('content',[])
    if content:
        text = content[0].get('text','')
        try:
            return json.loads(text)
        except:
            return {'text': text}
    return {'error': 'no content'}

# Step 1: Build dataset
print('=== BUILDING DATASET ===')
r = call_tool('lightning_build_dataset', {
    'agent_id': 'mcp-agents',
    'name': 'mhp-protocol-v3',
    'description': 'MHP Quality Protocol domain knowledge dataset for fine-tuning',
    'min_reward': 0.5,
})
print(json.dumps(r, indent=2))

dataset_id = r.get('dataset_id')
training_count = r.get('training_count', 0)
validation_count = r.get('validation_count', 0)
print(f"\nDataset ID: {dataset_id}")
print(f"Training examples: {training_count}")
print(f"Validation examples: {validation_count}")

if not dataset_id:
    print("ERROR: No dataset ID returned")
    sys.exit(1)

if training_count < 10:
    print(f"WARNING: Only {training_count} training examples, minimum 10 recommended")

# Step 2: Start training
print('\n=== STARTING TRAINING ===')
r = call_tool('lightning_start_training', {
    'dataset_id': dataset_id,
    'agent_id': 'mcp-agents',
})
print(json.dumps(r, indent=2))

training_id = r.get('training_run_id') or r.get('id')
status = r.get('status')
print(f"\nTraining Run ID: {training_id}")
print(f"Status: {status}")
