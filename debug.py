import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

METABASE_URL     = os.environ["METABASE_URL"].rstrip("/")
METABASE_API_KEY = os.environ["METABASE_API_KEY"]
DASHBOARD_ID     = 238

headers = {"Content-Type": "application/json", "x-api-key": METABASE_API_KEY}

print("=== Buscando estrutura do dashboard ===")
resp = requests.get(f"{METABASE_URL}/api/dashboard/{DASHBOARD_ID}", headers=headers, timeout=30)
resp.raise_for_status()
data = resp.json()

print(f"\nFiltros do dashboard:")
for p in data.get("parameters", []):
    print(f"  - id: {p.get('id')} | slug: {p.get('slug')} | name: {p.get('name')} | type: {p.get('type')}")

print(f"\nCards no dashboard:")
for dc in data.get("dashcards", []):
    card = dc.get("card", {})
    print(f"\n  Dashcard ID: {dc.get('id')} | Card ID: {card.get('id')} | Nome: {card.get('name')}")
    print(f"  Parameter mappings:")
    for pm in dc.get("parameter_mappings", []):
        print(f"    - parameter_id: {pm.get('parameter_id')} | target: {pm.get('target')}")
