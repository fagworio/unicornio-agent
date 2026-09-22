# Economia de contexto (contrato dos comandos)

> Referência do SKILL `unicorniohater-editor`. Aqui ficam os DETALHES dos
> comandos criados para cortar contexto sem afrouxar nenhum gate de qualidade
> (proveniência, subject, pHash, visão, 2/4/6, checklist e manifest continuam
> exatamente os mesmos). O QUE o agente precisa a cada execução está no SKILL.md.

## Por que isto existe

O desperdício não estava nos gates: estava na SESSÃO. Cada comando imprime um
JSON que entra na conversa e todo request seguinte relê a conversa inteira. Duas
sessões de 2 posts custam muito menos que uma de 6 — e a qualidade é a mesma,
porque o que muda é só *quando* a sessão termina.

## 1. Orçamento de sessão (hard cap de posts tocados)

Variáveis: `EDITOR_TARGET_READY_PER_RUN` (meta de READY, default 5),
`EDITOR_MAX_POSTS_TOUCHED_PER_RUN` (TETO de posts tocados, default 2; 0 desliga),
`EDITOR_SESSION_WINDOW_MINUTES` (default 90; PRECISA ser menor que o intervalo do
cron, senão a execução seguinte herda o teto esgotado),
`EDITOR_SESSION_CONTEXT_BYTES_BUDGET` (default 600000 bytes; 0 desliga).

- Ledger: `work/session_state.json` (`posts_touched`, `ready`, `context_bytes`).
  Expira sozinho por inatividade — a execução seguinte do cron começa limpa.
  Só comandos do agente editorial alimentam o ledger: `publish`/`publish-ready`
  rodam em outro cron no mesmo diretório e não podem estender esta janela.
  O arquivo é protegido por `flock` (leitura → incremento → escrita atômicos) e
  a vaga é reservada com `claim_touch` — concorrência não estoura o teto.
- `cards --compact` traz `session{target_ready, ready, max_posts_touched,
  remaining_posts, context_bytes_used, context_bytes_budget}` e corta o lote ao
  que ainda cabe. **HARD STOP**: com o orçamento de contexto estourado (ou o teto
  esgotado) o comando devolve `count: 0`, `cards: []` e `stop` ANTES de montar
  qualquer card — a sessão não "gasta só mais um pouquinho".
- `apply` de um post NOVO acima do teto devolve
  `status: session_budget_exhausted`, `wordpress_changed: false` e NÃO escreve
  nada (nenhum estado/attempt é consumido; o post fica para a próxima janela).
- Política explícita do orçamento estourado: **nenhum post novo**; o `apply`
  final de um post JÁ iniciado ainda é permitido (para não jogar fora o trabalho
  feito) e, depois dele, o `cards` seguinte já devolve o `stop`.
- O checklist NUNCA é simplificado para caber no orçamento: encerre a sessão.

## 2. `media-search-web` (compacto por padrão) e busca ADAPTATIVA

```bash
unicornio-editor media-search-web "TERMO" --post-id POST_ID --needed 2
```

- `--needed N` = déficit real do card (`images.missing`). A busca encerra assim
  que houver N candidatos **fortes** (`deterministic_match`) **distintos** e só
  expande entre engines enquanto faltar capacidade. `--limit` é apenas o teto de
  candidatos por engine (default 10; o alvo interno é `needed + 1`).
- A capacidade do SourceResolver usa o MESMO critério: **forte + frame distinto**
  (`verified_page` isolado não conta — provar que a imagem está na página não diz
  nada sobre relevância nem sobre diversidade). O funil roda POR CANDIDATO
  (resolver → evidência → pHash) e os candidatos dispensados aparecem separados
  em `capacity.deferred`/`deferred` (dispensado ≠ rejeitado).
- Saída (compacta): `capacity{needed,reuse,strong,accepted,missing,deferred}`,
  `coverage` (`local`/`mixed`/`web`), `decision` (`auto`/`choose`/`reuse`/`none`),
  `decision_id` (ledger), `select`, `options`, `rejected_summary` (contagem por
  motivo), `audit` (arquivo completo).
  `decision: auto` = um candidato inequívoco passou todos os hard gates: use
  `select` direto (não há julgamento a fazer). `choose` = empate/ambiguidade
  real: escolha entre 2-3 `options`. `reuse` = o acervo local cobriu a
  necessidade INTEIRA (nenhuma busca web foi feita). `none` = nada utilizável:
  registre `uncertain`.
- **`coverage` responde DE ONDE vieram as imagens** (`local`/`mixed`/`web`) e
  `decision` responde se o material da web exigiu julgamento. O caso misto
  (1 do acervo + 1 da web com `needed=2`) é `coverage: mixed` com decisão sobre o
  candidato da web — NUNCA `reuse`.
- Media Library/índice local vêm ANTES da web: `reuse` traz imagens já
  validadas (URL original + página de origem + pHash + media_id) que o apply
  aceita. Só o déficit restante vai aos buscadores — quando `missing` é 0,
  NENHUMA engine é consultada.
- Fonte do reuso: o índice casa por SUBJECT e, para o acervo antigo (entradas
  sem subject), a Media Library é buscada pelo subject e o índice dá a
  proveniência por `media_id`. Sem proveniência registrada não há reuso válido
  (o apply exige a URL original listada na página de origem).
- `--full` existe para auditoria (não use em produção: é o JSON grande).
- JSON completo (candidatos brutos, evidência, pHash, rejeitados um a um):
  `work/search/<termo>-<post_id>.json`.

## 3. Rework: `draft --for-fix` + `apply --merge-draft`

```bash
unicornio-editor draft POST_ID --for-fix          # só o componente + o erro
unicornio-editor apply POST_ID patch.json --merge-draft --compact
```

- `draft --for-fix` (ou `--component media|seo|text|trailer`) devolve apenas o
  componente que o gate bloqueou (inferido do `editorial.blocked.json`), o
  subject, a featured do plano e o erro. O artigo inteiro fica no arquivo
  (`full_draft` + `requires_content`) — nenhum rework de imagem precisa reenviar
  texto/SEO.
- `requires_content: false` é ENFORÇADO, não aconselhado: com o post BLOCKED num
  gate de mídia/SEO/trailer, `content POST_ID` devolve
  `status: content_not_required` (com o componente e o próximo passo) em vez de
  despejar o corpo. Para reescrever o texto de verdade, repita com `--force`
  (ou o gate do rework é de texto, ou o post já saiu do estado BLOCKED).
- `apply --merge-draft` trata o arquivo como PATCH PARCIAL: mescla
  deterministicamente com `editorial.draft.json` (dicionários chave a chave;
  listas substituem). O merge é do CÓDIGO, não do modelo; o resultado auditável
  fica em `backups/<id>/editorial.merged.json`.

## 4. `media-validate` (contrato compacto)

```json
{
  "valid": 3,
  "rejected": [{"index": 2, "reason": "..."}],
  "capacity": {"required": 4, "valid": 3, "missing": 1},
  "featured": {"status": "passed|rejected|absent", "reason": "..."},
  "audit": "work/media-validate/<post>.json"
}
```

O agente não carrega dados de sucesso: só o que falhou, quanto falta e como está
a featured. A visão detalhada/evidência fica no arquivo (`--full` para ver).

## 5. Dois produtos por comando pesado (regra arquitetural)

Todo comando pesado grava um arquivo detalhado para auditoria e devolve um
stdout pequeno orientado à próxima ação:

| comando | arquivo de auditoria |
|---|---|
| `cards --compact` | `work/cards.latest.json` |
| `media-search-web` | `work/search/<termo>-<post>.json` |
| `media-search-listicle` | `work/search/listicle-<obras>.json` |
| `media-validate` | `work/media-validate/<post>.json` |
| `apply --compact` | `backups/<id>/apply.latest.json` |

## 6. Telemetria: por comando, por post e por sessão

- `work/telemetry.jsonl` grava `cmd_output` com `command`, `kind`
  (`read`/`write`), `bytes`, `post_id` e os tamanhos de `cleaned_html`/draft.
- `unicornio-editor telemetry` → `context_bytes_by_command`, `by_post`,
  `post_context_detail`, `context_bytes_per_ready`, produção (READY, tocados).
- `unicornio-editor telemetry --sessions` cruza o ledger com o `state.db` do
  Hermes. Nomes precisos (o Hermes relê `input + cache_read + cache_write` a cada
  request): `prompt_tokens_per_ready`, `output_tokens_per_ready`,
  `total_model_tokens_per_ready`, `requests_per_ready`,
  `tool_context_bytes_per_ready` e `cost_per_ready_usd`. A unidade é POR READY
  (o volume/tipo de posts da janela varia, o custo por post pronto não).
- **O KPI oficial usa SOMENTE a fatia do cron** (`official_slice`:
  `run_source=cron` + id do job). Cada evento carrega `run_source`/`session_id`/
  `cron_job_id` (derivados do `HERMES_SESSION_ID`: `cron_<job>_...` = cron);
  execução manual/verificação aparece em `run_sources`, fora do baseline — antes
  ela contaminava o before/after.
- **`tool_context_bytes_per_ready` é a métrica PRINCIPAL de contexto**: ela mede
  o que o pipeline DEVOLVEU ao modelo, não o tamanho dos arquivos de auditoria
  (que ficam em disco e não entram na conversa).
- Mídia (mesma janela, mesma unidade): `local_reuse_rate` (reuso/necessidade),
  `web_searches_per_ready`, `vision_calls_per_ready`,
  `candidates_examined_per_ready`. `vision_calls` conta REQUISIÇÕES HTTP de visão
  (`vision_api_request`, uma por chamada: low e high contam separado) e a
  telemetria guarda `vision_input_tokens`/`vision_cached_tokens`/
  `vision_output_tokens` do `usage` do provedor — é o que reconcilia com o
  dashboard externo. `vision_low_requests`/`vision_high_requests` mostram quanto
  da escalada está sendo usada.
- **Numerador e denominador das MESMAS sessões**: cada evento carrega
  `root_session_id` e o KPI faz JOIN (`attribution: join_sessions`) — antes o
  numerador somava TODAS as sessões das últimas 24h enquanto o denominador
  (READY) era só dos eventos novos, inflando `prompt_tokens_per_ready`. Sem
  evento instrumentado o resultado cai para `attribution: window_job` e isso
  vem MARCADO.
- **main-loop x auxiliar x visão DIRETA**: `prompt_tokens_per_ready` é o
  MAIN-LOOP (tabela `sessions`); `aux_*` vem de `session_model_usage`
  (vision/compressão/título/aprovação do Hermes, que NÃO entram em `sessions`); e
  a visão do NOSSO Vision Gate é medida em `direct_vision` (evento
  `vision_api_request`), porque ela fala com o provedor por conta própria e não
  passa pelo accounting do Hermes. O `observed_grand_total` soma as três camadas
  (tokens e requests) — sem isso "total" não é total. **`cached_tokens` NÃO é
  somado a `prompt_tokens`** (no formato OpenAI ele já está incluído); somar
  duplicaria. O custo em USD cobre só as camadas do Hermes (a visão direta não tem
  preço no state.db — `grand_total_cost_partial` sinaliza isso).
- **Requests em camadas**: `main_requests` + `aux_requests` +
  `direct_vision_requests` = `grand_total_requests`; os limites do guard
  (`REQUEST_LIMIT`, `PROMPT_TOKEN_LIMIT`) comparam os TOTAIS, com as camadas
  expostas no JSON para diagnóstico.
- **Qualidade por decisão** (`decision_quality`): a decisão de mídia fica no
  ledger append-only `work/media_decisions.jsonl` — uma entrada por busca/ITEM,
  com `decision_id` (o mesmo id vai no `media_plan[]`, no `media_search_result` e
  no `media_validate_result`) — e os gates seguintes carregam essa decisão nos
  eventos. Assim dá para comparar `auto` x `choose` x `reuse` em
  `validate_rejected_items_per_event`, `validate_posts_with_rejection_rate`,
  `media_block_rate`, `ready_first_pass_share` (dos READY, quantos foram de
  primeira) e `first_pass_success_rate` (das **primeiras tentativas**, quantas
  deram READY: 1 READY + 10 bloqueados na primeira = 9,1%, não 100%).
- **O `decision_id` é a única fonte de verdade da medição**: `decision`,
  `score_gap`, `coverage` e `selected_url` saem do LEDGER pelo id — o texto que o
  agente copiou para o `media_plan` é ignorado para métrica (erro de cópia não
  pode virar medição). Cada item do `media_plan` emite um evento de
  `media-validate` com o estado da atribuição:
  `resolved` (id no ledger) | `missing` (item sem id) | `invalid` (id inexistente)
  | `mixed` (plano com mais de uma decisão, sem rótulo único). O resumo publica
  `decision_attribution` + `decision_attribution_rate` — sem isso a leitura de
  `auto` x `choose` pode ficar enviesada por itens não atribuídos.
- O arquivo legado `work/media_decisions.json` (mapa post → última decisão) só é
  usado para posts SEM registro no JSONL — o log novo é autoritativo.
- `EDITOR_AUTO_SCORE_MARGIN` (default 2) é a margem de `evidence_score` para o
  `auto`: **hipótese de calibração**, não fato. Se os casos `auto` passarem a ser
  rejeitados depois, suba a margem (ou exija `len(fortes) == 1`).
- Cada busca grava `media_search_result` (needed, reuse, strong, ambiguous,
  accepted, rejected, deferred, examined, engines_queried, decision,
  decision_reason, decision_id, coverage) e o artefato `work/search/*.json`
  guarda a decisão com RAZÃO e scores — é o que permite auditar a qualidade das
  imagens escolhidas.
- **Armadilha de instrumentação**: `log_event` descarta campo cujo NOME contenha
  `password`/`token`/`secret`/`authorization`/`cookie`/`api_key` QUANDO o valor é
  texto (proteção de credencial). Contadores numéricos (`input_tokens` etc.)
  passam normalmente; se um campo novo não aparecer no telemetry.jsonl, suspeite
  desse filtro antes de investigar o pipeline.
- Freios do monitor (`hermes/cost_guard.py`): além de USD (main + auxiliar),
  também `HERMES_EDITORIAL_WINDOW_REQUEST_LIMIT`,
  `HERMES_EDITORIAL_WINDOW_PROMPT_TOKEN_LIMIT` (input + cache_read +
  cache_write) e `HERMES_EDITORIAL_WINDOW_CONTEXT_BYTES_LIMIT` (0 = desligado).
  Todos filtram por `run_source=cron` + id do job: sessão MANUAL pesada não pode
  bloquear o cron. Estourado qualquer um, o monitor não acorda o LLM e a próxima
  janela começa limpa.
  `HERMES_EDITORIAL_WINDOW_INPUT_TOKEN_LIMIT` continua aceito como alias
  deprecated de PROMPT tokens.
- Edge case conhecido do Hermes (não é bug do monitor): num job NOVO/RECRIADO, a
  primeira observação (`last_hash is None`) executa o agente mesmo com a saída
  congelada; do segundo tick em diante o bloqueio não acorda o LLM.

## 7. `content POST_ID` só para reescrita real

O card traz `requires_content`: `false` em rework de mídia/SEO/trailer e em post
novo cujo texto existente serve. Leia o conteúdo **só** quando `requires_content`
for `true` ou quando você DECIDIR reescrever o texto (aí sim `content POST_ID`).

## Verificação local (antes de commitar)

- Rode a suíte também com ambiente LIMPO (`env -i PATH=/usr/bin:/bin HOME=/tmp/x
  .venv/bin/python -m unittest discover -s tests`): o CI do GitHub não tem nenhuma
  variável `HERMES_*`, e um teste que dependa de `HERMES_EDITORIAL_CRON_JOB_ID` ou
  de `HERMES_SESSION_ID` vazado do shell passa localmente e quebra no CI.
- Testes que medem fatia do cron/telemetria devem FIXAR a origem
  (`UNICORNIO_RUN_SOURCE` + `HERMES_*`), nunca depender do ambiente de quem roda.
