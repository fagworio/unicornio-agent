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
- Saída (compacta): `capacity{needed,reuse,strong,accepted,missing}`,
  `reuse[...]`, `decision` (`auto`/`choose`/`reuse`/`none`), `select`, `options`,
  `rejected_summary` (contagem por motivo), `audit` (arquivo completo).
  `decision: auto` = um candidato inequívoco passou todos os hard gates: use
  `select` direto (não há julgamento a fazer). `choose` = empate/ambiguidade
  real: escolha entre 2-3 `options`. `reuse` = o acervo local cobriu o déficit
  (nenhuma busca web foi feita). `none` = nada utilizável: registre `uncertain`.
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
  `candidates_examined_per_ready`. `vision_calls` conta CHAMADAS REAIS de visão
  (cache e bypass determinístico não contam).
- **Qualidade por decisão** (`decision_quality`): a decisão de mídia fica no
  ledger `work/media_decisions.json` e os gates seguintes carregam essa decisão
  nos eventos, então dá para comparar `auto` x `choose` x `reuse` em
  `validate_rejected_items_per_event`, `validate_posts_with_rejection_rate`,
  `media_block_rate` e `first_pass_ready_rate`. É a prova de que a economia de
  julgamento não piorou a imagem escolhida.
- `EDITOR_AUTO_SCORE_MARGIN` (default 2) é a margem de `evidence_score` para o
  `auto`: **hipótese de calibração**, não fato. Se os casos `auto` passarem a ser
  rejeitados depois, suba a margem (ou exija `len(fortes) == 1`).
- Cada busca grava `media_search_result` (needed, reuse, strong, ambiguous,
  accepted, rejected, deferred, examined, engines_queried, decision,
  decision_reason) e o artefato `work/search/*.json` guarda a decisão com RAZÃO
  e scores — é o que permite auditar a qualidade das imagens escolhidas.
- Freios do monitor (`hermes/cost_guard.py`): além de USD, também
  `HERMES_EDITORIAL_WINDOW_REQUEST_LIMIT`,
  `HERMES_EDITORIAL_WINDOW_INPUT_TOKEN_LIMIT` e
  `HERMES_EDITORIAL_WINDOW_CONTEXT_BYTES_LIMIT` (0 = desligado). Estourado
  qualquer um, o monitor não acorda o LLM e a próxima janela começa limpa.

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
