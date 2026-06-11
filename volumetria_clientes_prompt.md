# Automação: Monitoramento de Volumetria de Vidas Ativas vs. Contratadas

## Objetivo

Verificar semanalmente se cada cliente da Nilo está consumindo **90% ou mais** das vidas contratadas, analisar o comportamento dos **últimos 3 meses**, buscar a proposta de contrato no Google Drive, ler as respostas do CSM no thread da semana anterior e notificar o CSM responsável no Slack com contexto completo e próximos passos.

---

## Fontes de Dados

| Fonte | Detalhe |
|---|---|
| **Planilha de contratos** | Google Sheets ID: `1R2wdIX4AHQ5xtnl6CbiQlC83v4c-UAi2GwB0z5RYofQ` |
| **Metabase** | Dashboard CS: `https://metabase.niloservices.co/dashboard/238-dash-cs` |
| **Card Metabase** | Taxa de Atividade - Tabela (Card ID: 9263, Dashcard ID: 24154) |
| **Google Drive** | Pasta compartilhada: `Contas a Receber` |
| **Slack** | Canal privado: `#volumetria-de-clientes` |

### Colunas relevantes da planilha

| Coluna | Campo |
|---|---|
| Provider ID | Identificador único do cliente no Metabase |
| Cliente | Nome do cliente |
| Número de pacientes em contrato | Vidas contratadas |
| CSM (coluna H) | Nome do CSM responsável |

### Mapeamento de menções no Slack

| Valor na planilha | Menção no Slack |
|---|---|
| `Weslley Vilarinho` | `@Weslley Vilarinho` |
| `Caroline Mendes` | `@Caroline Mendes de Moraes` |

### Parâmetros Metabase

| Parâmetro | parameter_id | tipo | target |
|---|---|---|---|
| provider_id | `b46cc8b5` | `id` | `dimension` |
| start_date | `1c0cfe6c` | `date/single` | `variable` |

### Variáveis de Ambiente

| Secret | Descrição |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | JSON da Service Account com acesso ao Sheets e ao Drive |
| `METABASE_URL` | `https://metabase.niloservices.co` |
| `METABASE_API_KEY` | Chave de API do Metabase |
| `SLACK_BOT_TOKEN` | Token `xoxb-...` do bot Slack (scopes: `chat:write`, `groups:history`) |

---

## Fluxo de Execução

### Etapa 1 — Leitura da planilha

Ler todos os clientes com Provider ID e número de pacientes em contrato válidos. Capturar também o nome do cliente e o CSM responsável (coluna H). Ignorar linhas com contrato em branco ou `N/A`.

---

### Etapa 2 — Leitura do thread da semana anterior no Slack

Buscar no canal `#volumetria-de-clientes` a última mensagem principal postada pelo bot (identificada pelo texto "Volumetria de Clientes"). Se encontrada:

- Buscar todas as respostas no thread dessa mensagem
- Ignorar mensagens do próprio bot
- Registrar por cliente: autor, texto e timestamp de cada resposta humana
- Associar cada resposta ao cliente mencionado no thread correspondente

---

### Etapa 3 — Consulta ao Metabase

Para cada cliente, consultar o card **Taxa de Atividade - Tabela** usando o endpoint:

```
POST /api/dashboard/238/dashcard/24154/card/9263/query
```

- Passar `provider_id` e `start_date` = primeiro dia do mês atual
- A tabela retorna histórico completo com colunas: `month`, `active_patients`, `registered_patients`, `ratio`
- Se a resposta for HTTP 202, tratar como query assíncrona: fazer polling em `GET /api/async/{job_id}` até status `completed`
- Extrair `active_patients` dos últimos 3 meses disponíveis

---

### Etapa 4 — Análise dos últimos 3 meses

Calcular para cada mês:

```
taxa = (active_patients / contracted_lives) * 100
```

Classificar o padrão de consumo:

| Padrão | Critério | Emoji |
|---|---|---|
| **Recorrente** | Excedeu 90% nos 3 meses | 🔴 |
| **Crescente** | Excedeu 90% nos últimos 1 ou 2 meses | 🟡 |
| **Pontual** | Excedeu 90% apenas no mês atual | 🟠 |

---

### Etapa 5 — Busca do contrato no Google Drive

Executar somente para clientes que excederem 90% no mês atual.

**5.1 Localizar a pasta do cliente**
Buscar na pasta "Contas a Receber" uma subpasta com o nome do cliente (coluna "Cliente"). Busca case-insensitive.

**5.2 Selecionar o documento correto**

1. Arquivo com `renovação` ou `aditivo` no nome → pegar o mais recente por data de modificação
2. Se não houver → pegar o arquivo mais recente por data de modificação
3. Se houver apenas um arquivo na pasta → usar esse independente do nome

**5.3 Analisar o PDF**

Extrair o texto e procurar tabela de próximas faixas de vidas contratadas. Padrões comuns:

- Seção com título contendo `"próximas faixas"` ou `"precificação"`
- Tabela com colunas: `Faixas`, `Vidas Ativas`, `Valor/Vida Ativa`, `Bônus Conversa`
- Valores numéricos de faixas (ex: `1.000`, `2.000`, `3.000`) com preços associados

Identificar a próxima faixa aplicável acima do consumo atual. Extrair: número de vidas, valor mensal, custo por vida excedente.

---

### Etapa 6 — Notificação no Slack

**6.1 Mensagem principal**

Enviar via bot token para `#volumetria-de-clientes`. Guardar o `ts` retornado pela API para usar nos threads.

```
📊 *Volumetria de Clientes — {mês/ano}*
Clientes que atingiram *90% ou mais* do contrato de vidas:

{emoji_padrão} *{nome}* (Provider {id}) — {pct}% ({ativos} de {contrato} vidas)
{mês1}: {pct1}% | {mês2}: {pct2}% | {mês3}: {pct3}% → Padrão: {classificação}
```

Se nenhum cliente atingir o limiar:
```
📊 *Volumetria de Clientes — {mês/ano}*
✅ Nenhum cliente atingiu o limiar de 90% este mês.
```

**6.2 Reply em thread por cliente**

Para cada cliente que excedeu, postar resposta no thread da mensagem principal.

Se houver resposta do CSM na semana anterior, incluir antes do alerta:
```
💬 *Contexto da semana passada:*
{autor} respondeu: "{texto}"
```

**Cenário A — Próxima faixa encontrada:**
```
{menção_csm} — *{nome}* está consumindo {pct}% do contrato
({ativos} vidas ativas de {contrato} contratadas).

💬 Contexto da semana passada:
{autor} respondeu: "{texto}"

📄 Analisei o contrato mais recente e há uma próxima faixa prevista:
• Próxima faixa: {vidas} vidas
• Valor: R$ {valor}/mês
• Custo por vida excedente: R$ {custo}

Qual o próximo passo que deseja seguir? 🙂
```

**Cenário B — Sem próxima faixa:**
```
{menção_csm} — *{nome}* está consumindo {pct}% do contrato
({ativos} vidas ativas de {contrato} contratadas).

📄 Analisei o contrato mais recente e não há tabela de próximas faixas prevista.

Qual o próximo passo que deseja seguir? 🙂
```

**Cenário C — Pasta ou documento não encontrado:**
```
{menção_csm} — *{nome}* está consumindo {pct}% do contrato
({ativos} vidas ativas de {contrato} contratadas).

⚠️ Não encontrei a pasta ou contrato deste cliente em "Contas a Receber" no Drive.

Qual o próximo passo que deseja seguir? 🙂
```
