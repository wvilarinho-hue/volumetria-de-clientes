import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

METABASE_URL     = os.environ["METABASE_URL"].rstrip("/")
METABASE_API_KEY = os.environ["METABASE_API_KEY"]
CARD_ID          = 8623

headers = {"Content-Type": "application/json", "x-api-key": METABASE_API_KEY}

print("=== Buscando estrutura do card 8623 ===")
resp = requests.get(f"{METABASE_URL}/api/card/{CARD_ID}", headers=headers, timeout=30)
resp.raise_for_status()
data = resp.json()

print(f"\nNome: {data.get('name')}")
print(f"\nTemplate-tags:")
tags = data.get("dataset_query", {}).get("native", {}).get("template-tags", {})
for name, tag in tags.items():
    print(f"  - name: {name} | display_name: {tag.get('display-name')} | type: {tag.get('type')}")

print("\n=== Testando query com cliente de exemplo ===")
payload = {
    "parameters": [
        {
            "type": "category",
            "target": ["variable", ["template-tag", list(tags.keys())[0]]] if tags else [],
            "value": "Novamed"
        }
    ]
}
resp2 = requests.post(f"{METABASE_URL}/api/card/{CARD_ID}/query", headers=headers, json=payload, timeout=60)
print(f"Status: {resp2.status_code}")
result = resp2.json()
rows = result.get("data", {}).get("rows", [])
cols = result.get("data", {}).get("cols", [])
print(f"Colunas: {[c.get('name') for c in cols]}")
print(f"Primeiras linhas: {rows[:3]}")
