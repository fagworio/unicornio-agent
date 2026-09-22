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
- `cards --compact` traz `session{target_ready, ready, max_posts_touched,
  remaining_posts, context_bytes_used, context_bytes_budget}` e corta o lote ao
  que ainda cabe. Com `remaining_posts: 0` (ou `stop` preenchido) a sessão acabou.
- `apply` de um post NOVO acima do teto devolve
  `status: session_budget_exhausted`, `wordpress_changed: false` e NÃO escreve
  nada (nenhum estado/attempt é consumido; o post fica para a próxima janela).
- Reaplicar um post JÁ tocado é sempre permitido (é o mesmo post).
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
  (`full_draft`) — nenhum rework de imagem precisa reenviar texto/SEO.
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
  Hermes e devolve `tokens_per_ready`, `tokens_per_post_touched`,
  `requests_per_ready`, `tool_context_bytes_per_ready` e `cost_per_ready_usd`.
  É a medida para comparar antes/depois de cada mudança.
- Freios do monitor (`hermes/cost_guard.py`): além de USD, também
  `HERMES_EDITORIAL_WINDOW_REQUEST_LIMIT`,
  `HERMES_EDITORIAL_WINDOW_INPUT_TOKEN_LIMIT` e
  `HERMES_EDITORIAL_WINDOW_CONTEXT_BYTES_LIMIT` (0 = desligado). Estourado
  qualquer um, o monitor não acorda o LLM e a próxima janela começa limpa.

## 7. `content POST_ID` só para reescrita real

O card traz `requires_content`: `false` em rework de mídia/SEO/trailer e em post
novo cujo texto existente serve. Leia o conteúdo **só** quando `requires_content`
for `true` ou quando você DECIDIR reescrever o texto (aí sim `content POST_ID`).
