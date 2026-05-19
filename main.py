import os
import json
import requests
from datetime import date
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

SPREADSHEET_ID    = "1R2wdIX4AHQ5xtnl6CbiQlC83v4c-UAi2GwB0z5RYofQ"
METABASE_URL      = os.environ["METABASE_URL"].rstrip("/")
METABASE_API_KEY  = os.environ["METABASE_API_KEY"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
DASHBOARD_ID      = 238
CARD_NAME_KEYWORD = "taxa de atividade"
THRESHOLD         = 90.0

def get_start_date():
    return date.today().replace(day=1).isoformat()

def get_month_label(start_date):
    meses = {
        1:"janeiro",2:"fevereiro",3:"março",4:"abril",
        5:"maio",6:"junho",7:"julho",8:"agosto",
        9:"setembro",10:"outubro",11:"novembro",12:"dezembro"
    }
    d = date.fromisoformat(start_date)
    return f"{meses[d.month]}/{d.year}"

def get_spreadsheet_clients():
    sa_json = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(
        sa_json,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(SPREADSHEET_ID).sheet1

    all_values = sheet.get_all_values()
    if not all_values:
        return []

    headers = all_values[0]
    rows    = all_values[1:]

    try:
        idx_id       = next(i for i, h in enumerate(headers) if "provider id" in h.lower())
        idx_contract = next(i for i, h in enumerate(headers) if "pacientes em contrato" in h.lower())
    except StopIteration:
        raise RuntimeError(f"Colunas não encontradas. Cabeçalhos disponíveis: {headers}")

    clients = []
    for row in rows:
        raw_id       = str(row[idx_id]).strip()       if idx_id < len(row)       else ""
        raw_contract = str(row[idx_contract]).strip() if idx_contract < len(row) else ""

        if not raw_id or not raw_contract:
            continue

        try:
            contracted = int(float(raw_contract.replace(".", "").replace(",", ".")))
            if contracted <= 0:
                continue
            clients.append({"provider_id": raw_id, "contracted_lives": contracted})
        except (ValueError, TypeError):
            print(f"⚠️  Linha ignorada — provider={raw_id}, contrato={raw_contract}")

    return clients

def metabase_headers():
    return {"Content-Type": "application/json", "x-api-key": METABASE_API_KEY}

def find_card_id(dashboard_id, keyword):
    url  = f"{METABASE_URL}/api/dashboard/{dashboard_id}"
    resp = requests.get(url, headers=metabase_headers(), timeout=30)
    resp.raise_for_status()
    for dashcard in resp.json().get("dashcards", []):
        card = dashcard.get("card", {})
        if keyword.lower() in card.get("name", "").lower():
            return card["id"]
    return None

def query_active_patients(card_id, provider_id, start_date):
    url = f"{METABASE_URL}/api/card/{card_id}/query"
    payload = {
        "parameters": [
            {"type": "category",    "target": ["variable", ["template-tag", "provider_id"]], "value": provider_id},
            {"type": "date/single", "target": ["variable", ["template-tag", "start_date"]],  "value": start_date}
        ]
    }
    resp = requests.post(url, headers=metabase_headers(), json=payload, timeout=60)
    if resp.status_code != 200:
        print(f"⚠️  Metabase {resp.status_code} para provider {provider_id}")
        return None
    result    = resp.json()
    rows      = result.get("data", {}).get("rows", [])
    cols      = result.get("data", {}).get("cols", [])
    if not rows or not cols:
        return None
    col_index = next((i for i, c in enumerate(cols) if "active_patients" in c.get("name","").lower()), None)
    if col_index is None:
        return None
    try:
        return int(float(str(rows[0][col_index]).replace(",",".")))
    except (ValueError, IndexError):
        return None

def fmt(n):
    return f"{n:,}".replace(",", ".")

def build_message(alerts, start_date):
    month  = get_month_label(start_date)
    header = f":bar_chart: *Volumetria de Clientes — {month}*\nClientes que atingiram *90% ou mais* do contrato de vidas:\n\n"
    if not alerts:
        return header + "✅ Nenhum cliente atingiu o limiar de 90% este mês."
    lines = []
    for a in sorted(alerts, key=lambda x: -x["pct"]):
        emoji = ":red_circle:" if a["pct"] >= 100 else ":large_yellow_circle:"
        lines.append(
            f"{emoji} *Provider {a['provider_id']}* — {a['pct']}% "
            f"({fmt(a['active_patients'])} de {fmt(a['contracted_lives'])} vidas)"
        )
    return header + "\n".join(lines)

def send_to_slack(message):
    resp = requests.post(SLACK_WEBHOOK_URL, json={"text": message}, timeout=15)
    resp.raise_for_status()

def main():
    start_date = get_start_date()
    print(f"📅 Referência: {start_date}")

    print("📋 Lendo planilha...")
    clients = get_spreadsheet_clients()
    print(f"   {len(clients)} clientes encontrados.")

    print("🔍 Buscando card no Metabase...")
    card_id = find_card_id(DASHBOARD_ID, CARD_NAME_KEYWORD)
    if not card_id:
        raise RuntimeError(f"Card '{CARD_NAME_KEYWORD}' não encontrado no dashboard {DASHBOARD_ID}.")
    print(f"   Card ID: {card_id}")

    alerts = []
    for client in clients:
        pid        = client["provider_id"]
        contracted = client["contracted_lives"]
        print(f"   Provider {pid}...")
        active = query_active_patients(card_id, pid, start_date)
        if active is None:
            print(f"   ↳ Sem dados, pulando.")
            continue
        pct = round((active / contracted) * 100, 1)
        print(f"   ↳ {active}/{contracted} = {pct}%")
        if pct >= THRESHOLD:
            alerts.append({
                "provider_id":      pid,
                "active_patients":  active,
                "contracted_lives": contracted,
                "pct":              pct,
            })

    print(f"\n🚨 {len(alerts)} cliente(s) acima de {THRESHOLD}%.")
    print("📤 Enviando para o Slack...")
    send_to_slack(build_message(alerts, start_date))
    print("✅ Mensagem enviada com sucesso.")

if __name__ == "__main__":
    main()
