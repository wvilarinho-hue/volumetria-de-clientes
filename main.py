import os
import io
import json
import time
import requests
from datetime import date, datetime
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import pdfplumber
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

SPREADSHEET_ID      = "1R2wdIX4AHQ5xtnl6CbiQlC83v4c-UAi2GwB0z5RYofQ"
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
DRIVE_FOLDER_ID     = "1A0gNawgeF3JQfLllTiZFXyT6U-drFz1K"

# Mapeamento fixo para clientes cujo nome na planilha não bate com o nome da pasta no Drive
DRIVE_FOLDER_OVERRIDES = {
    "hospital da baleia":        "1dKpxeeLvFQDbOKChMdYm8aRmscPdI_61",
    "programa vivaz (florence)": "1HhihGSSyuVb7pYECSnS4SL_AxFfJITyf",
    "life":                      "1SSNZj0o-9eY8fQzeviFm7Sp-KOgE3enA",
}

CSM_MENTIONS = {
    "weslley vilarinho":  "<@U098G010EJV>",
    "caroline mendes":    "<@U0894RSCLTB>",
}

slack = WebClient(token=SLACK_BOT_TOKEN)

# ── Datas ─────────────────────────────────────────────────────────────────────

def get_months(n=3):
    """Retorna os primeiros dias dos últimos n meses."""
    today = date.today()
    return [
        (today - relativedelta(months=i)).replace(day=1).isoformat()
        for i in range(n - 1, -1, -1)
    ]

def get_month_label(iso_date):
    meses = {
        1:"janeiro",2:"fevereiro",3:"março",4:"abril",
        5:"maio",6:"junho",7:"julho",8:"agosto",
        9:"setembro",10:"outubro",11:"novembro",12:"dezembro"
    }
    d = date.fromisoformat(iso_date)
    return f"{meses[d.month]}/{d.year}"

def fmt(n):
    return f"{n:,}".replace(",", ".")

# ── Planilha ──────────────────────────────────────────────────────────────────

def get_spreadsheet_clients():
    sa_json = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(
        sa_json,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
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

# ── Slack — leitura de threads anteriores ─────────────────────────────────────

def get_previous_thread_replies():
    """
    Busca a última mensagem principal do bot no canal e retorna
    as respostas humanas de cada thread, indexadas pelo nome do cliente.
    """
    replies_by_client = {}
    try:
        result = slack.conversations_history(channel=SLACK_CHANNEL_ID, limit=50)
        messages = result.get("messages", [])

        # Encontra a última mensagem principal do bot com "Volumetria de Clientes"
        main_msg = next(
            (m for m in messages if "Volumetria de Clientes" in m.get("text", "")),
            None
        )
        if not main_msg:
            print("   ↳ Nenhuma mensagem anterior encontrada.")
            return replies_by_client

        thread_ts = main_msg.get("ts")
        thread = slack.conversations_replies(channel=SLACK_CHANNEL_ID, ts=thread_ts)
        thread_msgs = thread.get("messages", [])

        bot_id = slack.auth_test()["user_id"]

        for msg in thread_msgs:
            if msg.get("user") == bot_id:
                # Extrai o nome do cliente da mensagem do bot (está em negrito *nome*)
                import re
                match = re.search(r"\*(.+?)\*", msg.get("text", ""))
                if not match:
                    continue
                client_name = match.group(1)

                # Busca respostas humanas no subthread desta mensagem
                sub_ts = msg.get("ts")
                sub = slack.conversations_replies(channel=SLACK_CHANNEL_ID, ts=sub_ts)
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

    # Retorna dict {YYYY-MM: active_patients}
    data = {}
    for row in rows:
        row_month = str(row[date_idx])[:7]
        try:
            data[row_month] = int(float(str(row[active_idx]).replace(",", ".")))
        except (ValueError, TypeError):
            pass
    return data

# ── Análise dos 3 meses ───────────────────────────────────────────────────────

def classify_pattern(month_rates):
    """
    month_rates: lista de taxas dos últimos 3 meses, do mais antigo para o mais recente.
    """
    above = [r >= THRESHOLD for r in month_rates]
    if all(above):
        return "Recorrente 🔴"
    elif above[-1] and any(above[:-1]):
        return "Crescente 🟡"
    else:
        return "Pontual 🟠"

# ── Google Drive ──────────────────────────────────────────────────────────────

def get_drive_service():
    sa_json = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(
        sa_json,
        scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=creds)

def find_client_folder(service, parent_id, client_name):
    result = service.files().list(
        q=f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id, name)"
    ).execute()
    folders = result.get("files", [])
    name_lower = client_name.lower().strip()

    # 1. Match exato
    for f in folders:
        if f["name"].lower().strip() == name_lower:
            return f["id"]

    # 2. Nome da planilha contém o nome da pasta (ex: "Life" → "Life Saúde")
    for f in folders:
        if name_lower in f["name"].lower():
            return f["id"]

    # 3. Nome da pasta contém alguma palavra do nome da planilha (ex: "florence" ← "Programa Vivaz (Florence)")
    words = [w for w in name_lower.split() if len(w) > 3]
    for f in folders:
        folder_lower = f["name"].lower()
        if any(w in folder_lower for w in words):
            return f["id"]

    return None

def get_latest_contract(service, folder_id):
    result = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id, name, modifiedTime)",
        orderBy="modifiedTime desc"
    ).execute()
    files = result.get("files", [])
    if not files:
        return None

    # Prioridade 1: renovação ou aditivo
    priority = [
        f for f in files
        if any(kw in f["name"].lower() for kw in ["renovação", "renovacao", "aditivo"])
    ]
    if priority:
        return priority[0]

    # Prioridade 2: mais recente
    return files[0]

def download_file(service, file_id):
    request = service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buffer.seek(0)
    return buffer

def extract_next_tier(pdf_buffer, current_active):
    """
    Extrai a próxima faixa de vidas do PDF acima do consumo atual.
    Retorna dict com vidas, valor_mensal, custo_por_vida ou None.
    """
    try:
        with pdfplumber.open(pdf_buffer) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        if not row:
                            continue
                        # Procura linhas com valor numérico de faixa
                        first_cell = str(row[0] or "").replace(".", "").replace(",", "").strip()
                        try:
                            faixa = int(first_cell)
                        except ValueError:
                            continue

                        if faixa > current_active:
                            # Extrai valor por vida (última coluna numérica)
                            valor_vida = None
                            valor_mensal = None
                            for cell in reversed(row):
                                cell_str = str(cell or "").strip()
                                if "R$" in cell_str or cell_str.replace(".", "").replace(",", "").replace(" ", "").isdigit():
                                    cleaned = cell_str.replace("R$", "").replace(".", "").replace(",", ".").strip()
                                    try:
                                        val = float(cleaned)
                                        if valor_vida is None:
                                            valor_vida = val
                                        elif valor_mensal is None:
                                            valor_mensal = val
                                            break
                                    except ValueError:
                                        pass

                            return {
                                "vidas":         faixa,
                                "valor_mensal":  valor_mensal,
                                "custo_por_vida": valor_vida,
                            }
    except Exception as e:
        print(f"   ⚠️  Erro ao extrair faixas do PDF: {e}")
    return None

# ── Slack — envio ─────────────────────────────────────────────────────────────

def get_csm_mention(csm_name):
    return CSM_MENTIONS.get(csm_name.lower().strip(), f"@{csm_name}")

def post_main_message(text):
    result = slack.chat_postMessage(channel=SLACK_CHANNEL_ID, text=text, mrkdwn=True)
    return result["ts"]

def post_thread_reply(channel_id, thread_ts, text):
    slack.chat_postMessage(
        channel=channel_id,
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
        pattern_emoji = "🔴" if a["pct_current"] >= 100 else "🟡"
        history = " | ".join(
            f"{get_month_label(m)}: {r:.1f}%"
            for m, r in a["history"]
        )
        lines.append(
            f"{pattern_emoji} *{a['name']}* (Provider {a['provider_id']}) — {a['pct_current']:.1f}% "
            f"({fmt(a['active_current'])} de {fmt(a['contracted_lives'])} vidas)\n"
            f"{history} → Padrão: {a['pattern']}"
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
    drive_error = alert.get("drive_error")

    if drive_error:
        lines += [
            "⚠️ Não encontrei a pasta ou contrato deste cliente em \"Contas a Receber\" no Drive.",
            ""
        ]
    elif tier:
        valor_mensal  = f"R$ {tier['valor_mensal']:,.2f}/mês".replace(",", ".") if tier.get("valor_mensal") else "—"
        custo_por_vida = f"R$ {tier['custo_por_vida']:,.2f}".replace(",", ".") if tier.get("custo_por_vida") else "—"
        lines += [
            "📄 Analisei o contrato mais recente e há uma próxima faixa prevista:",
            f"• Próxima faixa: {fmt(tier['vidas'])} vidas",
            f"• Valor: {valor_mensal}",
            f"• Custo por vida excedente: {custo_por_vida}",
            ""
        ]
    else:
        lines += [
            "📄 Analisei o contrato mais recente e não há tabela de próximas faixas prevista.",
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

    print("\n📋 Lendo planilha...")
    clients = get_spreadsheet_clients()
    print(f"   {len(clients)} clientes encontrados.")

    print("\n💬 Buscando respostas do thread anterior no Slack...")
    channel_id       = SLACK_CHANNEL_ID
    previous_replies = get_previous_thread_replies()
    print(f"   {len(previous_replies)} resposta(s) encontrada(s).")

    print("\n🔍 Consultando Metabase...")
    drive_service    = get_drive_service()
    contas_folder_id = DRIVE_FOLDER_ID

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

        # Extrai taxas dos últimos 3 meses
        history = []
        for m in months:
            month_key = m[:7]
            active    = history_data.get(month_key, 0)
            rate      = round((active / contracted) * 100, 1)
            history.append((m, rate))

        current_month   = start_date[:7]
        active_current  = history_data.get(current_month, 0)
        pct_current     = round((active_current / contracted) * 100, 1)

        print(f"   ↳ {active_current}/{contracted} = {pct_current}%")

        if pct_current < THRESHOLD:
            continue

        pattern = classify_pattern([r for _, r in history])

        # Busca contrato no Drive
        next_tier   = None
        drive_error = False
        print(f"   ↳ Buscando contrato no Drive...")
        # Verifica override fixo primeiro
        override_id   = DRIVE_FOLDER_OVERRIDES.get(name.lower().strip())
        client_folder = override_id if override_id else find_client_folder(drive_service, contas_folder_id, name)
        if not client_folder:
            print(f"   ↳ Pasta não encontrada no Drive.")
            drive_error = True
        else:
            contract_file = get_latest_contract(drive_service, client_folder)
            if not contract_file:
                print(f"   ↳ Nenhum documento encontrado na pasta.")
                drive_error = True
            else:
                print(f"   ↳ Analisando: {contract_file['name']}")
                pdf_buffer = download_file(drive_service, contract_file["id"])
                next_tier  = extract_next_tier(pdf_buffer, active_current)
                if next_tier:
                    print(f"   ↳ Próxima faixa: {next_tier['vidas']} vidas")
                else:
                    print(f"   ↳ Sem tabela de faixas no contrato.")

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
            "drive_error":      drive_error,
        })

    print(f"\n🚨 {len(alerts)} cliente(s) acima de {THRESHOLD}%.")

    print("📤 Enviando mensagem principal no Slack...")
    main_text = build_main_message(alerts, start_date)
    main_ts   = post_main_message(main_text)
    print(f"   ✅ Mensagem enviada (ts: {main_ts})")

    if alerts:
        print("📤 Enviando threads por cliente...")
        for alert in alerts:
            previous = previous_replies.get(alert["name"])
            thread_text = build_thread_message(alert, previous)
            post_thread_reply(channel_id, main_ts, thread_text)
            print(f"   ✅ Thread: {alert['name']}")

    print("\n✅ Concluído.")

if __name__ == "__main__":
    main()
