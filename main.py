import os
import io
import json
import time
import requests
from datetime import date
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

SPREADSHEET_ID      = "1R2wdIX4AHQ5xtnl6CbiQlC83v4c-UAi2GwB0z5RYofQ"
CONTRACTS_SHEET_ID  = "1TZ_fqt1wI4hNwVQFQFz_sFN6TeTN2FmTeO3N3W5bPaE"
METABASE_URL        = os.environ["METABASE_URL"].rstrip("/")
METABASE_API_KEY    = os.environ["METABASE_API_KEY"]
SLACK_BOT_TOKEN     = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL_ID    = "C0B44KY9NGZ"
DASHBOARD_ID        = 238
DASHCARD_ID         = 24154
CARD_ID             = 9263
PROVIDER_PARAM_ID   = "b46cc8b5"
START_DATE_PARAM_ID = "1c0cfe6c"
THRESHOLD           = 90.0

CSM_MENTIONS = {
    "weslley vilarinho":  "<@U098G010EJV>",
    "caroline mendes":    "<@U0894RSCLTB>",
}

slack = WebClient(token=SLACK_BOT_TOKEN)

# ── Datas ─────────────────────────────────────────────────────────────────────

def get_months(n=3):
    today = date.today()
    return [
        (today - relativedelta(months=i)).replace(day=1).isoformat()
        for i in range(n - 1, -1, -1)
    ]

def get_month_label(iso_date):
    meses = {
        1:"jan",2:"fev",3:"mar",4:"abr",
        5:"mai",6:"jun",7:"jul",8:"ago",
        9:"set",10:"out",11:"nov",12:"dez"
    }
    d = date.fromisoformat(iso_date)
    return f"{meses[d.month]}/{d.year}"

def fmt(n):
    return f"{n:,}".replace(",", ".")

# ── Planilha de contratos (vidas contratadas + CSM) ───────────────────────────

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
        idx_name     = next(i for i, h in enumerate(headers) if h.lower() == "cliente")
        idx_csm      = next(i for i, h in enumerate(headers) if h.lower() == "csm")
    except StopIteration:
        raise RuntimeError(f"Colunas não encontradas. Cabeçalhos: {headers}")

    clients = []
    for row in rows:
        raw_id       = str(row[idx_id]).strip()       if idx_id < len(row)       else ""
        raw_contract = str(row[idx_contract]).strip() if idx_contract < len(row) else ""
        raw_name     = str(row[idx_name]).strip()     if idx_name < len(row)     else ""
        raw_csm      = str(row[idx_csm]).strip()      if idx_csm < len(row)      else ""

        if not raw_id or not raw_contract or raw_contract.upper() == "N/A":
            continue

        try:
            contracted = int(float(raw_contract.replace(".", "").replace(",", ".")))
            if contracted <= 0:
                continue
            clients.append({
                "provider_id":      raw_id,
                "contracted_lives": contracted,
                "name":             raw_name,
                "csm":              raw_csm,
            })
        except (ValueError, TypeError):
            print(f"⚠️  Linha ignorada — provider={raw_id}, contrato={raw_contract}")

    return clients

# ── Planilha de regras contratuais (faixas) ───────────────────────────────────

def get_contract_rules():
    """
    Lê a planilha de regras contratuais e retorna um dict indexado por provider_id.
    Cada entrada contém as faixas de vidas, valores e excedentes.
    """
    sa_json = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(
        sa_json,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(CONTRACTS_SHEET_ID).sheet1
    all_values = sheet.get_all_values()
    if not all_values:
        return {}

    headers = all_values[0]
    rows    = all_values[1:]

    def idx(keyword):
        for i, h in enumerate(headers):
            if keyword.lower() in h.lower():
                return i
        return None

    def parse_money(val):
        if not val or val.strip() in ("", "-", "N/A"):
            return None
        cleaned = str(val).replace("R$", "").replace(".", "").replace(",", ".").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None

    def parse_int(val):
        if not val or val.strip() in ("", "-", "N/A"):
            return None
        cleaned = str(val).replace(".", "").replace(",", "").strip()
        try:
            return int(float(cleaned))
        except ValueError:
            return None

    idx_pid = idx("provider_id")
    if idx_pid is None:
        print("⚠️  Coluna Provider_ID não encontrada na planilha de regras.")
        return {}

    rules = {}
    for row in rows:
        pid = str(row[idx_pid]).strip() if idx_pid < len(row) else ""
        if not pid:
            continue

        # Faixas: limites e valores
        tiers = []
        for n in range(1, 6):
            lim_idx = idx(f"faixa_{n}_limite")
            val_idx = idx(f"faixa_{n}_valor")
            if lim_idx is None:
                continue
            limite = parse_int(row[lim_idx] if lim_idx < len(row) else "")
            valor  = parse_money(row[val_idx] if val_idx is not None and val_idx < len(row) else "")
            if limite:
                tiers.append({"limite": limite, "valor_mensal": valor})

        exc_vida_idx = idx("valor_adicional_vida")
        exc_msg_idx  = idx("valor_adicional_msg")

        rules[pid] = {
            "tiers":             tiers,
            "excedente_vida":    parse_money(row[exc_vida_idx] if exc_vida_idx is not None and exc_vida_idx < len(row) else ""),
            "excedente_msg":     parse_money(row[exc_msg_idx]  if exc_msg_idx  is not None and exc_msg_idx  < len(row) else ""),
        }

    return rules

def find_next_tier(rules, provider_id, active_patients):
    """
    Retorna a próxima faixa contratual acima do consumo atual, ou None.
    """
    rule = rules.get(str(provider_id))
    if not rule or not rule["tiers"]:
        return None

    for tier in sorted(rule["tiers"], key=lambda t: t["limite"]):
        if tier["limite"] > active_patients:
            return {
                "vidas":          tier["limite"],
                "valor_mensal":   tier["valor_mensal"],
                "excedente_vida": rule.get("excedente_vida"),
            }
    return None

# ── Slack — leitura de threads anteriores ─────────────────────────────────────

def get_previous_thread_replies():
    replies_by_client = {}
    try:
        result   = slack.conversations_history(channel=SLACK_CHANNEL_ID, limit=50)
        messages = result.get("messages", [])

        main_msg = next(
            (m for m in messages if "Volumetria de Clientes" in m.get("text", "")),
            None
        )
        if not main_msg:
            print("   ↳ Nenhuma mensagem anterior encontrada.")
            return replies_by_client

        thread_ts = main_msg.get("ts")
        thread    = slack.conversations_replies(channel=SLACK_CHANNEL_ID, ts=thread_ts)
        bot_id    = slack.auth_test()["user_id"]

        import re
        for msg in thread.get("messages", []):
            if msg.get("user") == bot_id:
                match = re.search(r"\*(.+?)\*", msg.get("text", ""))
                if not match:
                    continue
                client_name = match.group(1)
                sub_ts = msg.get("ts")
                sub    = slack.conversations_replies(channel=SLACK_CHANNEL_ID, ts=sub_ts)
                for reply in sub.get("messages", [])[1:]:
                    if reply.get("user") != bot_id:
                        author = reply.get("username") or reply.get("user", "CSM")
                        text   = reply.get("text", "")
                        replies_by_client[client_name] = {"author": author, "text": text}

    except SlackApiError as e:
        print(f"⚠️  Erro ao buscar threads anteriores: {e}")

    return replies_by_client

# ── Metabase ──────────────────────────────────────────────────────────────────

def metabase_headers():
    return {"Content-Type": "application/json", "x-api-key": METABASE_API_KEY}

def fetch_async_result(job_id, max_retries=15, wait=3):
    for _ in range(max_retries):
        resp = requests.get(
            f"{METABASE_URL}/api/async/{job_id}",
            headers=metabase_headers(), timeout=30
        )
        if resp.status_code == 200:
            result = resp.json()
            if result.get("status") == "completed":
                return result
            elif result.get("status") == "failed":
                return None
        time.sleep(wait)
    return None

def query_active_patients(provider_id, start_date):
    url = f"{METABASE_URL}/api/dashboard/{DASHBOARD_ID}/dashcard/{DASHCARD_ID}/card/{CARD_ID}/query"
    payload = {
        "parameters": [
            {
                "id":     PROVIDER_PARAM_ID,
                "type":   "id",
                "target": ["dimension", ["template-tag", "provider_id"]],
                "value":  [str(provider_id)]
            },
            {
                "id":     START_DATE_PARAM_ID,
                "type":   "date/single",
                "target": ["variable", ["template-tag", "start_date"]],
                "value":  start_date
            }
        ]
    }

    resp = requests.post(url, headers=metabase_headers(), json=payload, timeout=60)

    if resp.status_code == 202:
        result = resp.json()
        job_id = result.get("id")
        if job_id:
            result = fetch_async_result(job_id)
            if not result:
                return None
    elif resp.status_code == 200:
        result = resp.json()
    else:
        print(f"   ⚠️  Metabase {resp.status_code} para provider {provider_id}")
        return None

    rows = result.get("data", {}).get("rows", [])
    cols = result.get("data", {}).get("cols", [])
    if not rows or not cols:
        return None

    col_names  = [c.get("name", "").lower() for c in cols]
    date_idx   = next((i for i, n in enumerate(col_names) if "month" in n or "date" in n), 0)
    active_idx = next((i for i, n in enumerate(col_names) if "active" in n), 1)

    data = {}
    for row in rows:
        row_month = str(row[date_idx])[:7]
        try:
            data[row_month] = int(float(str(row[active_idx]).replace(",", ".")))
        except (ValueError, TypeError):
            pass
    return data

# ── Análise ───────────────────────────────────────────────────────────────────

def classify_pattern(month_rates):
    above = [r >= THRESHOLD for r in month_rates]
    if all(above):
        return "Recorrente 🔴"
    elif above[-1] and any(above[:-1]):
        return "Crescente 🟡"
    else:
        return "Pontual 🟠"

# ── Slack — mensagens ─────────────────────────────────────────────────────────

def get_csm_mention(csm_name):
    return CSM_MENTIONS.get(csm_name.lower().strip(), f"@{csm_name}")

def post_main_message(text):
    result = slack.chat_postMessage(channel=SLACK_CHANNEL_ID, text=text, mrkdwn=True)
    return result["ts"]

def post_thread_reply(thread_ts, text):
    slack.chat_postMessage(
        channel=SLACK_CHANNEL_ID,
        thread_ts=thread_ts,
        text=text,
        mrkdwn=True
    )

def build_main_message(alerts, start_date):
    month  = get_month_label(start_date)
    header = f":bar_chart: *Volumetria de Clientes — {month}*\nClientes que atingiram *90% ou mais* do contrato de vidas:\n\n"
    if not alerts:
        return header + "✅ Nenhum cliente atingiu o limiar de 90% este mês."

    lines = []
    for a in sorted(alerts, key=lambda x: -x["pct_current"]):
        history_str = " | ".join(
            f"{get_month_label(m)}: {r:.1f}%"
            for m, r in a["history"]
        )
        emoji = "🔴" if a["pct_current"] >= 100 else "🟡"
        lines.append(
            f"{emoji} *{a['name']}* (Provider {a['provider_id']}) — {a['pct_current']:.1f}% "
            f"({fmt(a['active_current'])} de {fmt(a['contracted_lives'])} vidas)\n"
            f"{history_str} → Padrão: {a['pattern']}"
        )
    return header + "\n\n".join(lines)

def build_thread_message(alert, previous_reply):
    mention  = get_csm_mention(alert["csm"])
    name     = alert["name"]
    pct      = alert["pct_current"]
    active   = fmt(alert["active_current"])
    contract = fmt(alert["contracted_lives"])

    lines = [
        f"{mention} — *{name}* está consumindo {pct:.1f}% do contrato",
        f"({active} vidas ativas de {contract} contratadas).",
        ""
    ]

    if previous_reply:
        lines += [
            "💬 *Contexto da semana passada:*",
            f"{previous_reply['author']} respondeu: \"{previous_reply['text']}\"",
            ""
        ]

    tier = alert.get("next_tier")

    if tier:
        valor_mensal   = f"R$ {tier['valor_mensal']:,.2f}/mês".replace(",", "X").replace(".", ",").replace("X", ".") if tier.get("valor_mensal") else "—"
        excedente_vida = f"R$ {tier['excedente_vida']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if tier.get("excedente_vida") else "—"
        lines += [
            "📄 Analisei o contrato e há uma próxima faixa prevista:",
            f"• Próxima faixa: {fmt(tier['vidas'])} vidas",
            f"• Valor mensal: {valor_mensal}",
            f"• Custo por vida excedente: {excedente_vida}",
            ""
        ]
    else:
        lines += [
            "📄 Não há próxima faixa de vidas prevista no contrato.",
            ""
        ]

    lines.append("Qual o próximo passo que deseja seguir? 🙂")
    return "\n".join(lines)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    start_date = date.today().replace(day=1).isoformat()
    months     = get_months(3)
    print(f"📅 Mês atual: {start_date}")
    print(f"📅 Últimos 3 meses: {months}")

    print("\n📋 Lendo planilha de contratos...")
    clients = get_spreadsheet_clients()
    print(f"   {len(clients)} clientes encontrados.")

    print("\n📋 Lendo planilha de regras contratuais...")
    contract_rules = get_contract_rules()
    print(f"   {len(contract_rules)} regras carregadas.")

    print("\n💬 Buscando respostas do thread anterior no Slack...")
    previous_replies = get_previous_thread_replies()
    print(f"   {len(previous_replies)} resposta(s) encontrada(s).")

    print("\n🔍 Consultando Metabase...")
    alerts = []
    for client in clients:
        pid        = client["provider_id"]
        contracted = client["contracted_lives"]
        name       = client["name"]
        csm        = client["csm"]

        print(f"   {name} — Provider {pid}...")
        history_data = query_active_patients(pid, start_date)

        if not history_data:
            print(f"   ↳ Sem dados, pulando.")
            continue

        history = []
        for m in months:
            month_key = m[:7]
            active    = history_data.get(month_key, 0)
            rate      = round((active / contracted) * 100, 1)
            history.append((m, rate))

        current_month  = start_date[:7]
        active_current = history_data.get(current_month, 0)
        pct_current    = round((active_current / contracted) * 100, 1)

        print(f"   ↳ {active_current}/{contracted} = {pct_current}%")

        if pct_current < THRESHOLD:
            continue

        pattern   = classify_pattern([r for _, r in history])
        next_tier = find_next_tier(contract_rules, pid, active_current)

        if next_tier:
            print(f"   ↳ Próxima faixa: {next_tier['vidas']} vidas")
        else:
            print(f"   ↳ Sem próxima faixa no contrato.")

        alerts.append({
            "provider_id":      pid,
            "name":             name,
            "csm":              csm,
            "contracted_lives": contracted,
            "active_current":   active_current,
            "pct_current":      pct_current,
            "history":          history,
            "pattern":          pattern,
            "next_tier":        next_tier,
        })

    print(f"\n🚨 {len(alerts)} cliente(s) acima de {THRESHOLD}%.")

    print("📤 Enviando mensagem principal no Slack...")
    main_text = build_main_message(alerts, start_date)
    main_ts   = post_main_message(main_text)
    print(f"   ✅ Mensagem enviada (ts: {main_ts})")

    if alerts:
        print("📤 Enviando threads por cliente...")
        for alert in alerts:
            previous    = previous_replies.get(alert["name"])
            thread_text = build_thread_message(alert, previous)
            post_thread_reply(main_ts, thread_text)
            print(f"   ✅ Thread: {alert['name']}")

    print("\n✅ Concluído.")

if __name__ == "__main__":
    main()
