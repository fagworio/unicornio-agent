# Congelamento do experimento de contexto (Unicornio Agent)

Marco formal do início da coleta. A partir daqui **nenhuma mudança de código** no
pipeline nem na instrumentação — o próximo trabalho é apenas OBSERVAR os ciclos
reais do cron e comparar KPIs. Mudar qualquer coisa agora invalidaria o
experimento que acabou de ser instrumentado.

## Identificação

| Campo | Valor |
|---|---|
| SHA do CÓDIGO congelado | `cfa83758015a42c3b84ba79497f63ee9a5b4c1df` |
| Commit do registro (docs-only) | `dffe3eecdf963eebb504401cc03a581d53c8762a` (só acrescenta este documento; o tree de código é o de cfa8375) |
| Início da coleta (UTC) | 2026-09-22T15:54:51Z |
| Job do cron | `9e39343dc6f5` (job "UnicornioHater editorial pending", intervalo 120 min) |
| Primeira sessão cron válida | **pendente** — será a primeira sessão `cron_9e39343dc6f5_*` posterior a este marco que gere eventos com `run_source=cron`. Preencher aqui quando existir. |
| Atribuição esperada do KPI | `join_sessions` (exige correspondência COMPLETA das sessões; parcial cai em `window_job`, marcado) |
| Estado da fatia oficial no marco | 0 eventos atribuíveis (os anteriores não têm `run_source` e são contados como `unknown`) |
| CI verde do código congelado | run `35750568269` (dffe3ee) — 590 testes, compileall, SECRET_SCAN_PASSED, whitespace |

Nota de CI (para não gerar falso alarme): o run do próprio `cfa8375`
(`35750503497`) aparece FALHO no step "Check whitespace", mas por artefato de
publicação, não por conteúdo — aquele commit foi publicado com `--force-with-lease`
(amend de mensagem) e o step roda `git diff --check "$BEFORE_SHA...$HEAD_SHA"`,
onde o before-SHA deixa de existir no clone. O range equivalente
(`git diff --check 1da93c3...cfa8375`) passa limpo, e o run do commit seguinte com
o MESMO código passou em todos os steps. PITFALL do repo: não usar force-push/amend
em `main`; se usar, revalidar com `git diff --check <base>...HEAD` e considerar o
run do commit seguinte como o válido.

## Parâmetros em vigor no marco

Todos por default do código (nenhuma variável correspondente definida no `.env`):

| Parâmetro | Valor | Onde |
|---|---|---|
| `EDITOR_MAX_POSTS_TOUCHED_PER_RUN` | 2 (teto duro de posts por sessão) | teto |
| `EDITOR_TARGET_READY_PER_RUN` | 5 (meta, distribuída entre sessões) | meta |
| `EDITOR_MAX_POSTS_PER_RUN` | 5 | config.py:38 |
| `EDITOR_SESSION_WINDOW_MINUTES` | 90 (janela < intervalo do cron de 120 min) | config.py:53 |
| `EDITOR_SESSION_CONTEXT_BYTES_BUDGET` | 600000 | config.py:65 |
| `EDITOR_AUTO_SCORE_MARGIN` | 2 (**hipótese de calibração**, não fato) | config.py:61 |
| `EDITOR_POLICY_VERSION` | 2 | `.env` |
| `HERMES_EDITORIAL_WINDOW_REQUEST_LIMIT` | desligado | `.env`/guard |
| `HERMES_EDITORIAL_WINDOW_PROMPT_TOKEN_LIMIT` | desligado | `.env`/guard |
| `HERMES_EDITORIAL_WINDOW_CONTEXT_BYTES_LIMIT` | desligado | `.env`/guard |
| `HERMES_EDITORIAL_DAILY_COST_LIMIT_USD` | 0.80 | `.env` |

Com os freios de janela desligados, o teto que atua é o diário em USD (e o teto de
posts por sessão, que não é orçamento). O guard já mede as três camadas
(main + auxiliar + visão direta) e usa os TOTAIS.

## O que será observado (somente leitura)

`unicornio-editor telemetry --sessions` (fatia oficial = cron apenas):

- `grand_total_prompt_tokens_per_ready`
- `tool_context_bytes_per_ready`
- `grand_total_requests_per_ready`
- `grand_total_cost_per_ready_usd` (marcar `grand_total_cost_partial` quando houver visão direta)
- `first_pass_success_rate` (primeiras tentativas que viraram READY)
- `decision_attribution_rate` (itens com decisão resolvida no ledger)
- `auto` x `choose`: rejeição no `media-validate` e bloqueio de mídia no apply
- `web_searches_per_ready`
- `vision_low_requests` / `vision_high_requests` (e `direct_vision.tokens_partial`)
- `local_reuse_rate`, `candidates_examined_per_ready`

## Linhas de base anteriores ao marco (NÃO usar como comparativo oficial)

Números do período em que ainda não havia instrumentação por sessão — servem só
como referência histórica, porque misturam execuções manuais e não têm atribuição:

- 3 sessões do job, 247 requests main + 35 auxiliares = 282
- prompt tokens 34.946.774 (main) + 19.834 (aux) = 34.966.608
- custo US$ 0,1937 (main + auxiliar; visão direta sem accounting)
- `tool_context_bytes_per_ready` ~87-94 KB (contaminado por comandos manuais antes
  do isolamento por `run_source`; por isso **não** é baseline oficial)

## Regra do congelamento

1. Nenhuma alteração de código, parâmetro ou SKILL durante a coleta.
2. Leituras apenas por `telemetry --sessions` / `observability` (somente leitura).
3. Qualquer anomalia encontrada é REGISTRADA (data + evidência), não corrigida
   durante a coleta — a correção vira uma rodada posterior, com o experimento
   encerrado e o marco reaberto.
