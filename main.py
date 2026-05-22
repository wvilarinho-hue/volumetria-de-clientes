import os
import json
import time
import requests
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

# ──────────────────────────────────────────────
# Configuração
# ──────────────────────────────────────────────
SPREADSHEET_ID      = "1R2wdIX4AHQ5xtnl6CbiQlC83v4c-UAi2GwB0z5RYofQ"
METABASE_URL        = os.environ["METABASE_URL"].rstrip("/")
METABASE_API_KEY    = os.environ["METABASE_API_KEY"]
SLACK_BOT_TOKEN     = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL_ID    = "C0B47DKMKPT"   # #anti-churn
SLACK_TICKETS_CH    = "C027QG3D9MK"   # canal de tickets Zendesk

DASHBOARD_ID = 238
THRESHOLD    = -10.0  # queda igual ou superior a 10% → risco

# Cards do Dash CS
# card 9263 — Pacientes ativos
CARD_9263_DASHCARD   = 24154
CARD_9263_PARAM_PROVIDER   = "b46cc8b5"
CARD_9263_PARAM_START_DATE = "1c0cfe6c"

# card 6217 — Pacientes em linha de cuidado
CARD_6217_DASHCARD         = 11055
CARD_6217_PARAM_PROVIDER   = "8948a816"
CARD_6217_PARAM_START_DATE = "1c0cfe6c"

# card 2372 — Profissionais utilizando a plataforma
CARD_2372_DASHCARD         = 4122
CARD_2372_PARAM_PROVIDER   = "8948a816"
CARD_2372_PARAM_START_DATE = "1c0cfe6c"


# ──────────────────────────────────────────────
# Helper: descobrir dashcard IDs e param IDs
# ──────────────────────────────────────────────
def find_dashcard_ids():
    """
    Chame esta função UMA vez para descobrir os dashcard IDs e param IDs
    dos cards 6217 e 2372 dentro do dashboard 238.
    Cole os valores encontrados nas constantes acima e comente esta função.
    """
    url = f"{METABASE_URL}/api/dashboard/{DASHBOARD_ID}"
    resp = requests.get(url, headers=metabase_headers(), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    for dc in data.get("dashcards", []):
        card_id = dc.get("card_id") or (dc.get("card") or {}).get("id")
        if card_id in (6217, 2372):
            print(f"\n--- Card {card_id} ---")
            print(f"  dashcard_id : {dc['id']}")
            print(f"  parameter_mappings:")
            for pm in dc.get("parameter_mappings", []):
                print(f"    param_id={pm.get('parameter_id')}  target={pm.get('target')}")


# ──────────────────────────────────────────────
# Datas
# ──────────────────────────────────────────────
def get_periods():
    today         = date.today()
    inicio_atual  = today.replace(day=1)
    fim_atual     = today
    inicio_ant    = inicio_atual - relativedelta(days=60)
    fim_ant       = today        - relativedelta(days=60)
    inicio_zd     = today        - timedelta(days=15)
    return {
        "inicio_atual":  inicio_atual.isoformat(),
        "fim_atual":     fim_atual.isoformat(),
        "inicio_ant":    inicio_ant.isoformat(),
        "fim_ant":       fim_ant.isoformat(),
        "inicio_zd":     inicio_zd.isoformat(),
    }


# ──────────────────────────────────────────────
# Google Sheets — lista de providers
# ──────────────────────────────────────────────
def get_providers():
    sa_json = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds   = Credentials.from_service_account_info(
        sa_json,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    gc    = gspread.authorize(creds)
    sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
    rows  = sheet.get_all_values()
    if not rows:
        return []

    headers = rows[0]
    try:
        idx_id   = next(i for i, h in enumerate(headers) if "provider id" in h.lower())
        idx_name = next(i for i, h in enumerate(headers) if h.lower() == "cliente")
    except StopIteration:
        raise RuntimeError(f"Colunas não encontradas. Cabeçalhos: {headers}")

    providers = []
    for row in rows[1:]:
        pid  = str(row[idx_id]).strip()  if idx_id   < len(row) else ""
        name = str(row[idx_name]).strip() if idx_name < len(row) else ""
        if pid:
            providers.append({"provider_id": pid, "name": name})
    return providers


# ──────────────────────────────────────────────
# Metabase
# ──────────────────────────────────────────────
def metabase_headers():
    return {"Content-Type": "application/json", "x-api-key": METABASE_API_KEY}


def fetch_async(job_id, max_retries=10, wait=2):
    for _ in range(max_retries):
        r = requests.get(
            f"{METABASE_URL}/api/async/{job_id}",
            headers=metabase_headers(), timeout=30
        )
        if r.status_code == 200:
            result = r.json()
            if result.get("status") == "completed":
                return result
            if result.get("status") == "failed":
                return None
        time.sleep(wait)
    return None


def query_card(dashcard_id, card_id, param_provider, param_start_date,
               provider_id, start_date, end_date=None):
    """
    Consulta um card do dashboard CS e retorna o valor numérico principal.
    """
    if dashcard_id is None:
        print(f"  ⚠️  dashcard_id não configurado para card {card_id} — pulando")
        return None

    url = (f"{METABASE_URL}/api/dashboard/{DASHBOARD_ID}"
           f"/dashcard/{dashcard_id}/card/{card_id}/query")

    parameters = [
        {
            "id":     param_provider,
            "type":   "id",
            "target": ["dimension", ["template-tag", "provider_id"]],
            "value":  [str(provider_id)]
        },
        {
            "id":     param_start_date,
            "type":   "date/single",
            "target": ["variable", ["template-tag", "start_date"]],
            "value":  start_date
        }
    ]
    if end_date:
        parameters.append({
            "id":     "end_date",
            "type":   "date/single",
            "target": ["variable", ["template-tag", "end_date"]],
            "value":  end_date
        })

    resp = requests.post(url, headers=metabase_headers(),
                         json={"parameters": parameters}, timeout=60)

    if resp.status_code == 202:
        job_id = resp.json().get("id")
        result = fetch_async(job_id) if job_id else None
    elif resp.status_code == 200:
        result = resp.json()
    else:
        print(f"  ⚠️  Metabase {resp.status_code} — {resp.text[:200]}")
        return None

    if not result:
        return None

    rows = result.get("data", {}).get("rows", [])
    cols = result.get("data", {}).get("cols", [])
    if not rows or not cols:
        return None

    col_names  = [c.get("name", "").lower() for c in cols]
    active_idx = next(
        (i for i, n in enumerate(col_names)
         if "active" in n or "paciente" in n or "profissional" in n or "count" in n),
        1
    )

    # tenta retornar o valor do mês alvo; fallback para última linha
    target_month = start_date[:7]
    date_idx = next(
        (i for i, n in enumerate(col_names) if "date" in n or "month" in n or "mes" in n),
        None
    )
    if date_idx is not None:
        for row in rows:
            if str(row[date_idx])[:7] == target_month:
                try:
                    return int(float(str(row[active_idx]).replace(",", ".")))
                except (ValueError, TypeError):
                    return None

    try:
        return int(float(str(rows[-1][active_idx]).replace(",", ".")))
    except (ValueError, TypeError):
        return None


def get_three_indicators(provider_id, start_date, end_date=None):
    """Retorna dict com os 3 indicadores para um provider num período."""
    return {
        "pacientes_ativos": query_card(
            CARD_9263_DASHCARD, 9263,
            CARD_9263_PARAM_PROVIDER, CARD_9263_PARAM_START_DATE,
            provider_id, start_date, end_date
        ),
        "linha_cuidado": query_card(
            CARD_6217_DASHCARD, 6217,
            CARD_6217_PARAM_PROVIDER, CARD_6217_PARAM_START_DATE,
            provider_id, start_date, end_date
        ),
        "profissionais": query_card(
            CARD_2372_DASHCARD, 2372,
            CARD_2372_PARAM_PROVIDER, CARD_2372_PARAM_START_DATE,
            provider_id, start_date, end_date
        ),
    }


# ──────────────────────────────────────────────
# Cálculo de variação
# ──────────────────────────────────────────────
def calcular_variacao(atual, anterior):
    if anterior is None or anterior == 0:
        return None
    if atual is None:
        return None
    if atual == 0:
        return -100.0
    return round(((atual - anterior) / anterior) * 100, 1)


LABELS = {
    "pacientes_ativos": "Pacientes ativos",
    "linha_cuidado":    "Pacientes em linha de cuidado",
    "profissionais":    "Profissionais ativos",
}


# ──────────────────────────────────────────────
# Slack
# ──────────────────────────────────────────────
def slack_headers():
    return {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type":  "application/json",
    }


def slack_post(channel, text):
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers=slack_headers(),
        json={"channel": channel, "text": text},
        timeout=15,
    )
    data = resp.json()
    if not data.get("ok"):
        print(f"  ⚠️  Slack error: {data.get('error')}")
    return data


def slack_already_posted(provider_id, days=15):
    """Verifica se já foi postado alerta para este provider nos últimos N dias."""
    oldest = (date.today() - timedelta(days=days)).strftime("%s")
    resp = requests.get(
        "https://slack.com/api/conversations.history",
        headers=slack_headers(),
        params={"channel": SLACK_CHANNEL_ID, "oldest": oldest, "limit": 200},
        timeout=15,
    )
    data = resp.json()
    for msg in data.get("messages", []):
        if f"Provider ID: {provider_id}" in msg.get("text", ""):
            return True
    return False


def nivel_risco(maior_queda):
    if maior_queda <= -50:
        return "🔴", "Crítico"
    elif maior_queda <= -25:
        return "🟠", "Alto"
    else:
        return "🟡", "Atenção"


def build_alert(name, provider_id, atual, anterior, variacoes, periodos):
    quedas = {k: v for k, v in variacoes.items() if v is not None and v <= THRESHOLD}
    maior  = min(variacoes[k] for k in quedas)
    emoji, nivel = nivel_risco(maior)

    linhas = []
    for key, label in LABELS.items():
        a = atual.get(key)
        ant = anterior.get(key)
        var = variacoes.get(key)
        if a is None or ant is None:
            continue
        sinal = f"({var}%)" if var is not None else "(sem dados)"
        linhas.append(f"• {label}: {ant:,} → {a:,}  {sinal}".replace(",", "."))

    indicadores_em_risco = ", ".join(LABELS[k] for k in quedas)

    return (
        f"{emoji} *Risco de Churn — {name}* (Provider ID: {provider_id})\n\n"
        f"📊 *Variações nos últimos 60 dias:*\n"
        + "\n".join(linhas) + "\n\n"
        f"📅 Período atual:    {periodos['inicio_atual']} a {periodos['fim_atual']}\n"
        f"📅 Período anterior: {periodos['inicio_ant']} a {periodos['fim_ant']}\n\n"
        f"⚠️ Indicadores com queda ≥ 10%: {indicadores_em_risco}"
    )


# ──────────────────────────────────────────────
# Bloco Zendesk — lê canal Slack de tickets
# ──────────────────────────────────────────────
def get_tickets_from_slack(inicio_zd):
    oldest = str(int(time.mktime(
        time.strptime(inicio_zd, "%Y-%m-%d")
    )))
    resp = requests.get(
        "https://slack.com/api/conversations.history",
        headers=slack_headers(),
        params={"channel": SLACK_TICKETS_CH, "oldest": oldest, "limit": 1000},
        timeout=15,
    )
    mensagens = resp.json().get("messages", [])
    tickets = []
    for msg in mensagens:
        text = msg.get("text", "")
        if "novo ticket" not in text.lower():
            continue
        cliente   = ""
        categoria = ""
        assunto   = ""
        for linha in text.splitlines():
            l = linha.lower()
            if "cliente:" in l or "organização:" in l:
                cliente = linha.split(":", 1)[-1].strip()
            elif "categoria:" in l or "tipo:" in l:
                categoria = linha.split(":", 1)[-1].strip()
            elif "assunto:" in l or "título:" in l:
                assunto = linha.split(":", 1)[-1].strip()
        tickets.append({"cliente": cliente, "categoria": categoria, "assunto": assunto})
    return tickets


def build_zendesk_summary(tickets, periodos):
    if not tickets:
        return (
            f"🎫 *Chamados Zendesk — últimos 15 dias* "
            f"({periodos['inicio_zd']} a {periodos['fim_atual']})\n\n"
            "Nenhum ticket encontrado no período."
        )

    from collections import Counter
    clientes   = Counter(t["cliente"]   for t in tickets if t["cliente"])
    categorias = Counter(t["categoria"] for t in tickets if t["categoria"])

    top_clientes   = clientes.most_common(5)
    top_categorias = categorias.most_common(5)

    linhas_c = "\n".join(
        f"{i+1}. {c} — {n} chamados" for i, (c, n) in enumerate(top_clientes)
    )
    linhas_cat = "\n".join(
        f"{i+1}. {cat} — {n} chamados" for i, (cat, n) in enumerate(top_categorias)
    )

    return (
        f"🎫 *Chamados Zendesk — últimos 15 dias* "
        f"({periodos['inicio_zd']} a {periodos['fim_atual']})\n\n"
        f"👥 *Clientes que mais abriram chamados:*\n{linhas_c}\n\n"
        f"🏷️ *Categorias mais frequentes:*\n{linhas_cat}\n\n"
        f"📊 Total de chamados no período: {len(tickets)}"
    )


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main():
    periodos = get_periods()
    print(f"📅 Períodos: atual={periodos['inicio_atual']}→{periodos['fim_atual']} | "
          f"anterior={periodos['inicio_ant']}→{periodos['fim_ant']}")

    # ── Bloco 1: Metabase ──
    print("\n📋 Lendo providers da planilha...")
    providers = get_providers()
    print(f"  {len(providers)} providers encontrados.")

    alerts   = []
    saudaveis = 0

    for p in providers:
        pid  = p["provider_id"]
        name = p["name"]
        print(f"\n  {name} (Provider {pid})...")

        if slack_already_posted(pid):
            print("  ↳ Alerta recente já enviado, pulando.")
            continue

        atual    = get_three_indicators(pid, periodos["inicio_atual"], periodos["fim_atual"])
        anterior = get_three_indicators(pid, periodos["inicio_ant"],   periodos["fim_ant"])

        variacoes = {
            key: calcular_variacao(atual[key], anterior[key])
            for key in LABELS
        }

        quedas = {k: v for k, v in variacoes.items() if v is not None and v <= THRESHOLD}

        if quedas:
            print(f"  ↳ EM RISCO: {', '.join(LABELS[k] for k in quedas)}")
            msg = build_alert(name, pid, atual, anterior, variacoes, periodos)
            slack_post(SLACK_CHANNEL_ID, msg)
            alerts.append({"name": name, "provider_id": pid})
        else:
            saudaveis += 1
            print("  ↳ Saudável.")

    # ── Bloco 2: Zendesk via Slack ──
    print("\n🎫 Buscando tickets no canal Slack...")
    tickets = get_tickets_from_slack(periodos["inicio_zd"])
    print(f"  {len(tickets)} tickets encontrados.")
    slack_post(SLACK_CHANNEL_ID, build_zendesk_summary(tickets, periodos))

    # ── Bloco 3: Resumo ──
    top_ticket = ""
    if tickets:
        from collections import Counter
        top = Counter(t["cliente"] for t in tickets if t["cliente"]).most_common(1)
        if top:
            top_ticket = f"\n   Cliente com mais chamados: {top[0][0]} ({top[0][1]} tickets)"

    proxima = (date.today() + timedelta(days=15)).strftime("%d/%m/%Y")
    resumo  = (
        f"📋 *Monitor de Churn — {date.today().strftime('%d/%m/%Y')}*\n\n"
        f"🔴 *Queda de uso (Metabase):*\n"
        f"   Providers analisados: {len(providers)} | "
        f"Em risco: {len(alerts)} | Saudáveis: {saudaveis}\n\n"
        f"🎫 *Volume de chamados (Zendesk):*\n"
        f"   Tickets nos últimos 15 dias: {len(tickets)}"
        f"{top_ticket}\n\n"
        f"⏭️ Próxima execução: {proxima}"
    )
    slack_post(SLACK_CHANNEL_ID, resumo)
    print("\n✅ Monitoramento concluído.")


if __name__ == "__main__":
    # Para descobrir dashcard IDs dos cards 6217 e 2372, rode:
    # find_dashcard_ids()
    main()
