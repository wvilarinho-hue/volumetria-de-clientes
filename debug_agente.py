import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

METABASE_URL     = os.environ["METABASE_URL"].rstrip("/")
METABASE_API_KEY = os.environ["METABASE_API_KEY"]
CARD_ID          = 8623

headers = {"Content-Type": "application/json", "x-api-key": METABASE_API_KEY}

print("=== Consultando card 8623 sem filtros ===")
resp = requests.post(
    f"{METABASE_URL}/api/card/{CARD_ID}/query",
    headers=headers,
    json={"parameters": []},
    timeout=60
)
print(f"Status: {resp.status_code}")

if resp.status_code == 202:
    import time
    job_id = resp.json().get("id")
    print(f"Job assíncrono: {job_id}, aguardando...")
    for _ in range(15):
        time.sleep(3)
        r2 = requests.get(f"{METABASE_URL}/api/async/{job_id}", headers=headers, timeout=30)
        if r2.status_code == 200 and r2.json().get("status") == "completed":
            resp = r2
            break

result = resp.json()
rows = result.get("data", {}).get("rows", [])
cols = result.get("data", {}).get("cols", [])

print(f"\nColunas ({len(cols)}):")
for i, c in enumerate(cols):
    print(f"  [{i}] {c.get('name')}")

print(f"\nTotal de linhas: {len(rows)}")
print(f"\nPrimeiras 5 linhas:")
for row in rows[:5]:
    print(f"  {row}")
