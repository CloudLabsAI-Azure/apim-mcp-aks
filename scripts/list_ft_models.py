import json

models = json.load(open('models_eastus2.json'))

# Show all capability keys from first model to understand structure
if models:
    sample = models[0]
    print("Sample model structure keys:", list(sample.keys()))
    print("Sample model.model keys:", list(sample.get('model', {}).keys()))
    caps = sample.get('model', {}).get('capabilities', {})
    print("Sample capabilities keys:", list(caps.keys()))
    print()

# Search for fine-tune related capabilities
ft = []
for m in models:
    caps = m.get('model', {}).get('capabilities', {})
    for k, v in caps.items():
        if 'fine' in k.lower() or 'tune' in k.lower():
            ft.append(m)
            break

print(f"Models with fine-tune capabilities: {len(ft)}")
for m in sorted(ft, key=lambda x: x['model']['name']):
    name = m['model']['name']
    ver = m['model'].get('version', 'N/A')
    caps = m['model'].get('capabilities', {})
    ft_caps = {k: v for k, v in caps.items() if 'fine' in k.lower() or 'tune' in k.lower()}
    print(f"  {name:35s} version={ver:25s} {ft_caps}")

# Also check for gpt-4o-mini specifically
print("\n--- gpt-4o-mini entries ---")
for m in models:
    if 'gpt-4o-mini' in m.get('model', {}).get('name', ''):
        name = m['model']['name']
        ver = m['model'].get('version', 'N/A')
        caps = m['model'].get('capabilities', {})
        print(f"  {name}  version={ver}  caps={json.dumps(caps, indent=2)}")
