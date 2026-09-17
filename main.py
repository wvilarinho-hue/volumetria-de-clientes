import os
import json
import time
import requests
from datetime import date
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

# ── Configurações ─────────────────────────────────────────────────────────────
NEW_SPREADSHEET_ID  = "1j-cznF553ejWOKPXsIPr8MUgscfOxW1rs944VqFXW2A"
METABASE_URL        = os.environ["METABASE_URL"].rstrip("/")
METABASE_API_KEY    = os.environ["METABASE_API_KEY"]
SLACK_BOT_TOKEN     = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL_ID    = "C0B44KY9NGZ"
METABASE_CARD_ID    = 8623
THRESHOLD           = 50.0

CSM_MENTIONS = {
    "weslley vilarinho":                       "<@U098G010EJV>",
    "caroline de almeida mendes de moraes":    "<@U0894RSCLTB>",
    "caroline mendes":                         "<@U0894RSCLTB>",
}

# Índices das colunas no card 8623 (conforme debug)
COL_CLIENTE               = 0
COL_VIDAS_ATIVAS          = 4   # Vidas Ativas M-2
COL_VIDAS_ATIVAS_M1       = 5   # Vidas Ativas M-1
COL_VIDAS_MONITORADAS     = 6   # Vidas Monitoradas M-2
COL_VIDAS_MONITORADAS_M1  = 7   # Vidas Monitoradas M-1
COL_CHAT                  = 8   # Sessoes Chat M-2
COL_CHAT_M1               = 9   # Sessoes Chat M-1
COL_AGENTE                = 16  # Atendimentos Agente M-2
COL_AGENTE_M1             = 17  # Atendimentos Agente M-1

slack = WebClient(token=SLACK_BOT_TOKEN)

# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt(n):
    return f"{n:,}".replace(",", ".")

def get_week_label():
    today = date.today()
    return today.strftime("%d/%m/%Y")

def get_csm_mention(csm_name):
    return CSM_MENTIONS.get(csm_name.lower().strip(), f"@{csm_name}")

def normalize(name):
    """Normaliza nome para comparação fuzzy."""
    import unicodedata, re
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^a-z0-9\s]", "", name.lower())
    return name.strip()

def parse_number(val):
    if not val or str(val).strip() in ("", "-", "N/A", "None"):
        return None
    try:
        cleaned = str(val).replace("R$", "").replace(".", "").replace(",", ".").strip()
        result = float(cleaned)
        return int(result) if result == int(result) else result
    except (ValueError, TypeError):
        return None

# ── Planilha ──────────────────────────────────────────────────────────────────

def get_clients_from_spreadsheet():
    sa_json = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(
        sa_json,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    gc = gspread.authorize(creds)

    # Lê a aba principal (primeira aba com dados de clientes)
    sheet = gc.open_by_key(NEW_SPREADSHEET_ID).get_worksheet(0)
    all_values = sheet.get_all_values()

    if not all_values:
        return []

    # Debug: mostra primeiras linhas para entender estrutura
    print("   DEBUG — primeiras 15 linhas (primeiras 5 colunas):")
    for i, row in enumerate(all_values[:15]):
        preview = [str(c).strip()[:20] for c in row[:5]]
        print(f"   [{i}] {preview}")

    # Encontra a linha de cabeçalho onde "Clientes" está nas primeiras 5 colunas
    header_row_idx = None
    for i, row in enumerate(all_values):
        if not row:
            continue
        first_cells = [str(c).strip().lower() for c in row[:5]]
        if "clientes" in first_cells:
            col_pos = next(j for j, c in enumerate(first_cells) if c == "clientes")
            print(f"   Cabeçalho encontrado na linha {i + 1}, coluna {col_pos}.")
            header_row_idx = i
            break

    if header_row_idx is None:
        # Busca em qualquer posição para debug
        for i, row in enumerate(all_values):
            for j, cell in enumerate(row):
                if str(cell).strip().lower() == "clientes":
                    print(f"   'Clientes' encontrado na linha {i+1}, coluna {j} — fora do esperado.")
        raise RuntimeError("Cabeçalho 'Clientes' não encontrado nas primeiras 5 colunas.")

    headers = [str(h).strip() for h in all_values[header_row_idx]]
    rows    = all_values[header_row_idx + 1:]

    def idx(keyword):
        for i, h in enumerate(headers):
            if keyword.lower() in h.lower():
                return i
        return None

    idx_nome      = idx("clientes")
    idx_csm       = idx("csm responsável") or idx("csm")
    idx_regra     = idx("regra de vidas")
    idx_vidas_tot = idx("vidas totais")
    idx_franq_conv = idx("franquia conversas")
    idx_franq_age  = idx("franquia atendimentos")

    print(f"   Colunas encontradas: nome={idx_nome}, csm={idx_csm}, "
          f"regra={idx_regra}, vidas={idx_vidas_tot}, "
          f"conversas={idx_franq_conv}, agente={idx_franq_age}")

    clients = []
    for row in rows:
        if not row or idx_nome is None or idx_nome >= len(row):
            continue

        nome = str(row[idx_nome]).strip()
        if not nome or nome.lower() in ("clientes", "total de clientes:"):
            continue

        csm        = str(row[idx_csm]).strip()         if idx_csm       and idx_csm < len(row)       else ""
        regra      = str(row[idx_regra]).strip()        if idx_regra     and idx_regra < len(row)      else ""
        vidas_tot  = parse_number(row[idx_vidas_tot])   if idx_vidas_tot and idx_vidas_tot < len(row)  else None
        franq_conv = parse_number(row[idx_franq_conv])  if idx_franq_conv and idx_franq_conv < len(row) else None
        franq_age  = parse_number(row[idx_franq_age])   if idx_franq_age and idx_franq_age < len(row)  else None

        # Só inclui clientes com pelo menos uma métrica para monitorar
        if not any([vidas_tot, franq_conv, franq_age]):
            continue

        clients.append({
            "nome":       nome,
            "csm":        csm,
            "regra":      regra.lower(),   # "vidas ativas" ou "vidas navegadas"
            "vidas_tot":  vidas_tot,
            "franq_conv": franq_conv,
            "franq_age":  franq_age,
        })

    return clients

# ── Metabase ──────────────────────────────────────────────────────────────────

def metabase_headers():
    return {"Content-Type": "application/json", "x-api-key": METABASE_API_KEY}

def fetch_async_result(job_id, max_retries=20, wait=3):
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
                print(f"   ⚠️  Job falhou: {result.get('error')}")
                return None
        time.sleep(wait)
    print("   ⚠️  Timeout aguardando job assíncrono.")
    return None

def query_metabase_all_clients():
    """
    Consulta o card 8623 sem filtros e retorna dict indexado por nome normalizado.
    """
    resp = requests.post(
        f"{METABASE_URL}/api/card/{METABASE_CARD_ID}/query",
        headers=metabase_headers(),
        json={"parameters": []},
        timeout=60
    )

    if resp.status_code == 202:
        job_id = resp.json().get("id")
        if job_id:
            print(f"   ↳ Query assíncrona, aguardando job {job_id}...")
            result = fetch_async_result(job_id)
        else:
            result = resp.json()
    elif resp.status_code == 200:
        result = resp.json()
    else:
        print(f"   ⚠️  Metabase {resp.status_code}")
        return {}

    if not result:
        return {}

    rows = result.get("data", {}).get("rows", [])
    print(f"   ↳ {len(rows)} linhas retornadas do Metabase.")

    data = {}
    for row in rows:
        if not row or COL_CLIENTE >= len(row):
            continue
        nome = str(row[COL_CLIENTE] or "").strip()
        if not nome:
            continue

        def safe_int(idx):
            if idx >= len(row) or row[idx] is None:
                return None
            try:
                return int(float(str(row[idx])))
            except (ValueError, TypeError):
                return None

        data[normalize(nome)] = {
            "nome_original":      nome,
            "vidas_ativas":       safe_int(COL_VIDAS_ATIVAS),
            "vidas_ativas_m1":    safe_int(COL_VIDAS_ATIVAS_M1),
            "vidas_monitoradas":  safe_int(COL_VIDAS_MONITORADAS),
            "vidas_monit_m1":     safe_int(COL_VIDAS_MONITORADAS_M1),
            "chat":               safe_int(COL_CHAT),
            "chat_m1":            safe_int(COL_CHAT_M1),
            "agente":             safe_int(COL_AGENTE),
            "agente_m1":          safe_int(COL_AGENTE_M1),
        }

    return data

def find_metabase_client(metabase_data, client_name):
    """Busca o cliente no Metabase por nome, com matching progressivo."""
    norm_name = normalize(client_name)

    # 1. Match exato
    if norm_name in metabase_data:
        return metabase_data[norm_name]

    # 2. Planilha contém o nome do Metabase
    for key, val in metabase_data.items():
        if key in norm_name or norm_name in key:
            return val

    # 3. Match por primeira palavra significativa (>3 chars)
    words = [w for w in norm_name.split() if len(w) > 3]
    for key, val in metabase_data.items():
        if any(w in key for w in words):
            return val

    return None

# ── Slack ─────────────────────────────────────────────────────────────────────

def get_previous_thread_replies():
    replies = {}
    try:
        result   = slack.conversations_history(channel=SLACK_CHANNEL_ID, limit=50)
        messages = result.get("messages", [])
        main_msg = next(
            (m for m in messages if "Volumetria de Clientes" in m.get("text", "")),
            None
        )
        if not main_msg:
            return replies

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
                sub = slack.conversations_replies(channel=SLACK_CHANNEL_ID, ts=msg.get("ts"))
                for reply in sub.get("messages", [])[1:]:
                    if reply.get("user") != bot_id:
                        replies[client_name] = {
                            "author": reply.get("username") or reply.get("user", "CSM"),
                            "text":   reply.get("text", "")
                        }
    except SlackApiError as e:
        print(f"⚠️  Erro ao buscar threads: {e}")
    return replies

def build_main_message(alerts, week_label):
    header = (
        f":bar_chart: *Volumetria de Clientes — semana de {week_label}*\n"
        f"Clientes com consumo ≥ 70% em pelo menos uma métrica:\n\n"
    )
    if not alerts:
        return header + "✅ Nenhum cliente atingiu o limiar de 70% esta semana."

    lines = []
    for a in sorted(alerts, key=lambda x: -x["max_pct"]):
        metrics_str = []
        for m in a["metrics"]:
            bar   = "🔴" if m["pct"] >= 90 else "🟡"
            trend = m.get("trend", "")
            metrics_str.append(
                f"    {bar} *{m['label']}*: {m['pct']:.1f}%{trend} "
                f"({fmt(m['consumed'])} de {fmt(m['contracted'])})"
            )
        lines.append(
            f"*{a['nome']}* — CSM: {a['csm']}\n" + "\n".join(metrics_str)
        )

    return header + "\n\n".join(lines)

def build_thread_message(alert, previous_reply):
    mention = get_csm_mention(alert["csm"])
    nome    = alert["nome"]

    lines = [f"{mention} — *{nome}*", ""]

    if previous_reply:
        lines += [
            "💬 *Contexto da semana passada:*",
            f"{previous_reply['author']} respondeu: \"{previous_reply['text']}\"",
            ""
        ]

    lines.append("📊 *Consumo atual:*")
    for m in alert["metrics"]:
        bar   = "🔴" if m["pct"] >= 90 else "🟡"
        trend = m.get("trend", "")
        prev  = f" (mês anterior: {m['pct_prev']:.1f}%)" if m.get("pct_prev") is not None else ""
        lines.append(
            f"  {bar} {m['label']}: {m['pct']:.1f}%{trend}{prev} "
            f"({fmt(m['consumed'])} de {fmt(m['contracted'])})"
        )

    lines += ["", "Qual o próximo passo que deseja seguir? 🙂"]
    return "\n".join(lines)

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

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    week_label = get_week_label()
    print(f"📅 Semana de referência: {week_label}")

    print("\n📋 Lendo planilha de clientes...")
    clients = get_clients_from_spreadsheet()
    print(f"   {len(clients)} clientes com métricas para monitorar.")

    print("\n📊 Consultando Metabase (card 8623)...")
    metabase_data = query_metabase_all_clients()
    print(f"   {len(metabase_data)} clientes no Metabase.")

    print("\n💬 Buscando threads anteriores no Slack...")
    previous_replies = get_previous_thread_replies()
    print(f"   {len(previous_replies)} resposta(s) encontrada(s).")

    print("\n🔍 Cruzando dados...")
    alerts = []
    for client in clients:
        nome  = client["nome"]
        mb    = find_metabase_client(metabase_data, nome)

        if not mb:
            print(f"   ⚠️  {nome} — não encontrado no Metabase, pulando.")
            continue

        metrics_alert = []

        def calc_metric(label, consumed_now, consumed_prev, contracted):
            if consumed_now is None or not contracted:
                return None
            pct_now  = round((consumed_now / contracted) * 100, 1)
            pct_prev = round((consumed_prev / contracted) * 100, 1) if consumed_prev is not None else None
            delta    = round(pct_now - pct_prev, 1) if pct_prev is not None else None
            if delta is None:
                trend = ""
            elif delta > 2:
                trend = f" ↑ +{delta}pp"
            elif delta < -2:
                trend = f" ↓ {delta}pp"
            else:
                trend = f" → {delta:+.1f}pp"
            print(f"   {nome} | {label}: {consumed_now}/{contracted} = {pct_now}%{trend}")
            if pct_now >= THRESHOLD:
                return {
                    "label":      label,
                    "consumed":   consumed_now,
                    "contracted": contracted,
                    "pct":        pct_now,
                    "pct_prev":   pct_prev,
                    "delta":      delta,
                    "trend":      trend,
                }
            return None

        # Vidas
        if client["vidas_tot"]:
            if "navegadas" in client["regra"]:
                m = calc_metric("Vidas navegadas", mb["vidas_monitoradas"], mb["vidas_monit_m1"], client["vidas_tot"])
            else:
                m = calc_metric("Vidas ativas", mb["vidas_ativas"], mb["vidas_ativas_m1"], client["vidas_tot"])
            if m: metrics_alert.append(m)

        # Conversas
        if client["franq_conv"]:
            m = calc_metric("Conversas", mb["chat"], mb["chat_m1"], client["franq_conv"])
            if m: metrics_alert.append(m)

        # Agente
        if client["franq_age"]:
            m = calc_metric("Atendimentos Agente", mb["agente"], mb["agente_m1"], client["franq_age"])
            if m: metrics_alert.append(m)

        if metrics_alert:
            max_pct = max(m["pct"] for m in metrics_alert)
            alerts.append({
                "nome":    nome,
                "csm":     client["csm"],
                "metrics": metrics_alert,
                "max_pct": max_pct,
            })

    print(f"\n🚨 {len(alerts)} cliente(s) com pelo menos uma métrica ≥ {THRESHOLD}%.")

    print("📤 Enviando mensagem principal no Slack...")
    main_text = build_main_message(alerts, week_label)
    main_ts   = post_main_message(main_text)
    print(f"   ✅ Mensagem enviada (ts: {main_ts})")

    if alerts:
        print("📤 Enviando threads por cliente...")
        for alert in alerts:
            previous    = previous_replies.get(alert["nome"])
            thread_text = build_thread_message(alert, previous)
            post_thread_reply(main_ts, thread_text)
            print(f"   ✅ Thread: {alert['nome']}")

    print("\n✅ Concluído.")

if __name__ == "__main__":
    main()
