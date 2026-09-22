# Congelamento do experimento de contexto (Unicornio Agent)

Marco formal do início da coleta. A partir daqui **nenhuma mudança de código** no
pipeline nem na instrumentação — o próximo trabalho é apenas OBSERVAR os ciclos
reais do cron e comparar KPIs. Mudar qualquer coisa agora invalidaria o
experimento que acabou de ser instrumentado.

## Identificação

| Campo | Valor |
|---|---|
| SHA do CÓDIGO congelado | `cfa83758015a42c3b84ba79497f63ee9a5b4c1df` |
| Commit do registro (docs-only) | começa em `dffe3eecdf963eebb504401cc03a581d53c8762a`; commits posteriores só editam este documento — o tree de CÓDIGO congelado continua sendo o de `cfa8375` |
| Congelamento / experimento ARMADO (UTC) | 2026-09-22T15:54:51Z |
| Estado da coleta | `ARMED_WAITING_FOR_WORK` — assinatura do monitor = `0` (fila sem post elegível), verificado em 2026-09-22T16:44:56Z |
| Início efetivo da amostra (`active_since`) | **pendente** — só existe quando a primeira sessão cron VÁLIDA rodar (critério determinístico abaixo) |
| `first_valid_session` | **pending** |
| Job do cron | `9e39343dc6f5` (job "UnicornioHater editorial pending", intervalo 120 min) |
| Primeira sessão cron válida | **pendente** — preencher aqui quando existir. O nome `cron_9e39343dc6f5_*` sozinho NÃO basta: ver "Critério de sessão cron VÁLIDA". |
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

## Snapshot do runtime implantado (fora do Git)

O Git prova o código Python; não prova qual SKILL, monitor e references o Hermes estava
efetivamente lendo. Esta tabela é a evidência do runtime no marco t0 — **evidência para
auditoria, não hard gate**: nenhum destes hashes bloqueia execução ou publicação.

| artefato implantado (fora do Git) | sha256 implantado | bytes | mtime (UTC) | fonte no repo | sha256 da fonte | relacao esperada |
|---|---|---|---|---|---|---|
| `~/.hermes/skills/unicorniohater-editor/SKILL.md` | `3ad86dcebb0a25ce22c36868259043d4b1029e8b203c0a1f978c7e28b1cb46f5` | 9188 | 2026-09-22T16:01:52Z | `hermes/SKILL.md` | `3ad86dcebb0a25ce22c36868259043d4b1029e8b203c0a1f978c7e28b1cb46f5` | idêntico byte a byte |
| `~/.hermes/skills/unicorniohater-editor/references/politica-imagens.md` | `ce6874b21221a799fb139061d463de80c35524004b75c42cf7ad9276edf5826f` | 7815 | 2026-09-22T16:01:52Z | `hermes/references/politica-imagens.md` | `ce6874b21221a799fb139061d463de80c35524004b75c42cf7ad9276edf5826f` | idêntico byte a byte |
| `~/.hermes/skills/unicorniohater-editor/references/operacao.md` | `f56ae5012df12177e9f79857d423b905d8541b19289c17bad8703827dbab531f` | 7530 | 2026-09-22T16:01:52Z | `hermes/references/operacao.md` | `f56ae5012df12177e9f79857d423b905d8541b19289c17bad8703827dbab531f` | idêntico byte a byte |
| `~/.hermes/skills/unicorniohater-editor/references/economia-contexto.md` | `5b655c34b6e14fdf27eb6b806c56856bb878388972ea7443b1f301dd83117891` | 16430 | 2026-09-22T16:01:52Z | `hermes/references/economia-contexto.md` | `5b655c34b6e14fdf27eb6b806c56856bb878388972ea7443b1f301dd83117891` | idêntico byte a byte |
| `~/.hermes/skills/unicorniohater-editor/references/hash-imagens-analise.md` | `236c724e9c92861cc9e7b0f1cbbfe61482cf81897a018cc1403e9656dc4d3976` | 5523 | 2026-09-22T16:01:52Z | `hermes/references/hash-imagens-analise.md` | `236c724e9c92861cc9e7b0f1cbbfe61482cf81897a018cc1403e9656dc4d3976` | idêntico byte a byte |
| `~/.hermes/skills/unicorniohater-editor/references/editorial-texto.md` | `15a03059c5098e4de52c45d4b0db08ec9d4dd1d9474aa5a57739e29038939f53` | 2826 | 2026-09-22T16:01:52Z | `hermes/references/editorial-texto.md` | `15a03059c5098e4de52c45d4b0db08ec9d4dd1d9474aa5a57739e29038939f53` | idêntico byte a byte |
| `~/.hermes/skills/unicorniohater-editor/references/congelamento-experimento.md` | `a79f5d84712643b244695eb68169f799c67c97b5d524c4d46e71b8c2be308f23` | 16792 | 2026-09-22T17:27:39Z | `hermes/references/congelamento-experimento.md` | `a79f5d84712643b244695eb68169f799c67c97b5d524c4d46e71b8c2be308f23` | hoje idêntico à fonte em `main` no commit `a0b3fd2` (o que foi deployado); no t0 era `88eff3b8…` (4944 B). **Re-sincronizado por decisão do operador** em 2026-09-22T17:27:39Z — ver "Registros durante a coleta" |
| `~/.hermes/skills/unicorniohater-editor/scripts/commons_search.py` | `1b21922360b08e2e99ea0d4f3c6036390d35265da47b0f3096e24af6f453da96` | 3259 | 2026-09-17T08:00:15Z | (sem fonte versionada) | - | sem origem no Git; candidato divergente em `work/commons_search.py` (`work/` é git-ignored) |
| `~/.hermes/skills/unicorniohater-editor/scripts/precheck_media.py` | `e276a9eacc419c7a0c1c05536b1dc53386fa379f2ae5ff35d5aa81e665408fe4` | 2766 | 2026-09-16T07:29:57Z | (sem fonte versionada) | - | sem origem no Git; mesmo sha256 de `/tmp/precheck_media.py` (arquivo transiente) |
| `~/.hermes/scripts/unicornio-editor-monitor.sh` | `8b8d72a78066d5f8ea9c5f0389bbaacbda84c84b9a2dec5f7f97d21563b382d3` | 3780 | 2026-09-22T16:01:52Z | `hermes/monitor.sh` | `6d7c09107b504cfc3d12dd7c9e998ac907a83263bf44715611f160a69c771aaf` | **derivado**: `sed 's\|@PROJECT_ROOT@\|/www/wwwroot/hermes/unicornio-agent\|g'`; sha256 do resultado == sha256 implantado (CONFERE) |

Definição do job (também fora do Git):

| campo do job | valor |
|---|---|
| job_id / nome | `9e39343dc6f5` / UnicornioHater editorial pending |
| schedule | `every 120m` (intervalo 120 min) |
| skill carregada | `unicorniohater-editor` (de `~/.hermes/skills/`) |
| workdir | `/www/wwwroot/hermes/unicornio-agent` |
| monitor_script | `unicornio-editor-monitor.sh` |
| deliver | `telegram:5006103160` |
| prompt | 914 bytes, sha256 `f1a8850285299a3cae32681121c4a6fbdb91e59e02c8b3005c4a64766ac8f50c` |

Notas do snapshot (as três primeiras explicam por que a relação esperada não é
simplesmente "hash igual"):

- `.env` **não** é hasheado de propósito: contém credenciais (o hash de um arquivo de
segredos é informação derivada de segredo). A integridade dele é atestada pela lista de
parâmetros em vigor na seção anterior.
- O monitor tem `source_sha256` ≠ `deployed_sha256` porque existe transformação legítima:
a única permitida é `@PROJECT_ROOT@` → caminho absoluto do projeto. O que foi conferido
é o hash do **resultado** da transformação contra o hash implantado, não uma inspeção
visual.
- `scripts/commons_search.py` e `scripts/precheck_media.py` da skill **não têm origem no
Git** (instalados à mão antes do marco). Ficam registrados por hash: se mudarem, é
alteração de runtime durante a coleta e deve ser REGISTRADA, não corrigida.
- `SKILL.md` cita `scripts/diagnostico.sh`, que resolve contra o **workdir do job**
(`/www/wwwroot/hermes/unicornio-agent/scripts/diagnostico.sh`) e não contra a pasta
`scripts/` da skill — os dois `.py` acima não são o que o SKILL referencia.
- Mtime não é conteúdo: os arquivos de `~/.hermes` foram regravados em
2026-09-22T16:01:52Z e de novo em 2026-09-22T17:27:39Z (execuções do `cron-install.sh`),
**depois** do congelamento. O conteúdo, porém, é byte a byte o do t0 (hash conferido contra
a revisão no Git) — foi redeploy sem mudança, não alteração. É exatamente por isso que este
snapshot existe.
- Este registro (docs-only) é re-sincronizado no runtime **apenas por decisão explícita do
operador**, e cada re-sincronização entra em "Registros durante a coleta" com data + hash.
Última: 2026-09-22T17:27:39Z, `88eff3b8…` → `a79f5d84…`. Nunca em silêncio: enquanto a
cópia implantada estiver atrasada em relação a `main`, isso está escrito aqui.

## Critério de sessão cron VÁLIDA (início efetivo da amostra)

Uma sessão só inicia a amostra se TODAS as condições valerem:

1. início posterior a `freeze_at` (2026-09-22T15:54:51Z);
2. nome `cron_9e39343dc6f5_*`;
3. eventos de telemetria com `run_source == cron` **e** `cron_job_id == 9e39343dc6f5`;
4. ao menos um evento PRODUTIVO do pipeline: `post_started`, `apply_ready`,
 `apply_blocked` ou `media_search_result`;
5. o `root_session_id` da telemetria existe no `state.db` (pré-requisito do
 `join_sessions`).

Sessão vazia, só de housekeeping ou só com leitura de `cards` **não** inicia a amostra.
Quando a primeira sessão válida existir: preencher `active_since` e `first_valid_session`
na tabela de identificação e mudar o estado para `ACTIVE`.

```text
freeze_at 15:54:51Z ── ARMADO
      │
 tick do cron (120 min)
      │
 monitor encontrou trabalho?
      ├── não ──► continua ARMADO (idle, custo zero, nenhuma sessão)
      └── sim ──► sessão cron com evento produtivo + sessão no state.db
                      │
                 ACTIVE (active_since = início dessa sessão)
```

Estado em 2026-09-22T17:27:39Z: `ARMED_WAITING_FOR_WORK` — assinatura do monitor `0`.
Última sessão do job: `cron_9e39343dc6f5_20260922_090524` (12:05:24Z, 3h49 antes do
congelamento); próximo tick **19:27:39Z** (o `cron edit` de 2026-09-22T17:27:39Z rebaseou o
intervalo a partir da edição — antes era 18:01:52Z; ver "Registros durante a coleta").

## Como contar a amostra (denominadores)

Não usar horas desde o congelamento como tamanho de amostra: com a fila vazia o cron pode
tickar em ARMADO indefinidamente e "24 horas de experimento" pode significar 22 horas
ociosas com uma única execução útil. Denominadores válidos:

- sessões cron válidas;
- posts READY na fatia oficial (`run_source=cron`);
- posts tocados;
- itens de mídia com atribuição resolvida.

Os KPIs `*_per_ready` já são normalizados por READY; "quantas observações" vem dos
denominadores acima, nunca do relógio.

## Qual otimização escolher (só depois de ACTIVE, nunca antes)

A instrumentação já cobre as camadas (main + auxiliar + visão direta). A escolha do alvo
sai dos dados da primeira sessão válida, não de nova hipótese:

| sintoma medido | alvo |
|---|---|
| `context_bytes` domina | payload de tools (cards, candidatos de mídia) |
| `context_bytes` baixo + `prompt_tokens` alto | sessão/replay do Hermes (histórico, compressão, loop) |
| `web_searches_per_ready` alto | SourceResolver / caminho de busca |
| visão alta | economia de candidatos / escalonamento |
| `first_pass_success_rate` baixo | gates e rework |
| `auto` pior que `choose` | `EDITOR_AUTO_SCORE_MARGIN` |

Enquanto a amostra estiver vazia, o trabalho na branch é **preparar alternativas**, não
escolher uma.

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
4. Este documento é o registro do experimento: pode ser ESTENDIDO durante a coleta
   (docs-only, evidência) e re-sincronizado para `~/.hermes/.../references/` **somente por
   decisão explícita do operador**, com data + hash em "Registros durante a coleta".
   Código, parâmetro e SKILL seguem intocados.
5. `hermes/cron-install.sh` só pode ser executado do checkout de PRODUÇÃO — nunca de um
   worktree/branch (ver o pitfall registrado em "Registros durante a coleta").

## Registros durante a coleta (docs-only, com data e evidência)

Registrar não é corrigir: nada aqui invalida o congelamento de `cfa8375`, porque nenhum
item é código, parâmetro de pipeline ou SKILL. O que muda (quando muda) é o estado de
runtime, e isso fica escrito.

### 2026-09-22T17:27:39Z — `cron-install.sh` executado do checkout de produção (decisão do operador)

Comando: `cd /www/wwwroot/hermes/unicornio-agent && ./hermes/cron-install.sh`
(saída: `Updated job: 9e39343dc6f5`, workdir `/www/wwwroot/hermes/unicornio-agent`).

Efeitos medidos (snapshot antes/depois por sha256):

| arquivo | antes | depois | veredito |
|---|---|---|---|
| `~/.hermes/scripts/unicornio-editor-monitor.sh` | `8b8d72a7…` | `8b8d72a7…` | conteúdo idêntico (só mtime) |
| `~/.hermes/skills/.../SKILL.md` | `3ad86dce…` | `3ad86dce…` | conteúdo idêntico (só mtime) |
| `references/` (5 arquivos) | inalterados | inalterados | conteúdo idêntico (só mtime) |
| `references/congelamento-experimento.md` | `88eff3b8…` (4944 B) | `a79f5d84…` (16792 B) | **única mudança de conteúdo** — este registro, por decisão explícita |
| `.env` | `6f9a454d…` | `6f9a454d…` | conteúdo idêntico (o job id regravado com o mesmo valor) |
| job `9e39343dc6f5` | schedule/prompt/skill/workdir/monitor iguais | iguais | `prompt f1a88502…`, `workdir /www/wwwroot/hermes/unicornio-agent`, `monitor unicornio-editor-monitor.sh` |
| `next_run_at` do job | `2026-09-22T18:01:52Z` | `2026-09-22T19:27:39Z` | **rebased pelo `cron edit`** (intervalo de 120 min recalculado a partir da edição) — o tick das 18:01Z foi absorvido |

Veredito: o experimento continua válido (código, parâmetros e SKILL intocados; o monitor
segue `8b8d72a7…` e responde `0`), mas o próximo tick atrasou ~1h26. Registrado aqui para
que a leitura de "quando a amostra começou" não dependa de memória.

### Pitfall descoberto no mesmo dia — `cron-install.sh` de dentro de um worktree

`cron-install.sh` deriva `ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"` e passa
`--workdir "$ROOT"` para o `hermes cron edit`. Rodá-lo de
`/www/wwwroot/hermes/unicornio-agent-next` **reapontaria o job de produção para a branch
dev** — ou seja, o cron "congelado" passaria a executar código da branch sem nenhum merge.

Evidência do efeito colateral (teste com o monitor apontado para o worktree, que não tem
`.env` porque é git-ignored):

```text
$ sed 's|@PROJECT_ROOT@|/www/wwwroot/hermes/unicornio-agent-next|g' hermes/monitor.sh > /tmp/monitor_next_test.sh
$ bash /tmp/monitor_next_test.sh
/tmp/monitor_next_test.sh: line 29: ./.env: No such file or directory
exit=1
```

Ou seja: além de contaminar a coleta, o editorial pararia (monitor sem assinatura estável e
sessão sem credenciais/config). Por isso a regra 5 acima.

## Isolamento da próxima fase (branch + worktree)

Regra: **nenhum desenvolvimento na árvore de produção.** `/www/wwwroot/hermes/unicornio-agent`
fica em `main` intocada (é o workdir do cron) durante toda a coleta.

```text
/www/wwwroot/hermes/unicornio-agent       -> main congelada (produção; cron aponta aqui)
/www/wwwroot/hermes/unicornio-agent-next  -> branch next/context-optimization (dev)
```

O `next` foi criado em 2026-09-22 como **worktree do mesmo repositório**, a partir de
`56aef0c`, com `.venv` próprio (CPython 3.11, uv) e editable install apontando para o
próprio worktree. Conferido: `.venv/bin/python -c "import unicornio_editor"` na produção
continua resolvendo para `/www/wwwroot/hermes/unicornio-agent/src/`, e nenhum arquivo do
`.venv` de produção referencia o `next`.

Proibições no worktree `next` (qualquer uma delas contaminaria a coleta):

- `pip install -e` / `uv pip install -e` do `next` **no ambiente Python do cron** — isso
  faria o job "congelado" executar código da branch sem nenhum merge;
- rodar `hermes/cron-install.sh` (cria/edita job, copia SKILL, instala monitor);
- qualquer escrita em `~/.hermes/skills/`, `~/.hermes/scripts/` ou `~/.hermes/cron/`;
- qualquer alteração no `.env` de produção (usar `.env` próprio no worktree);
- merge/deploy enquanto a coleta estiver ARMADA ou ATIVA.

Gate local, equivalente ao CI (o workflow só dispara em push para `main`). Conferido em
2026-09-22T16:45:38Z–16:45:46Z no worktree `next`:

```bash
cd /www/wwwroot/hermes/unicornio-agent-next
env -i PATH=/usr/bin:/bin HOME=/tmp/unicornio-ci .venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m compileall -q src tests scripts
.venv/bin/python scripts/check_repository.py
git diff --check origin/main...HEAD
```

Resultado: `Ran 590 tests in 7.181s` / `OK` (env limpo, sem `HERMES_*`), `compileall` OK,
`SECRET_SCAN_PASSED`, `git diff --check origin/main...HEAD` limpo.

Checklist de promoção (conferir ANTES e DEPOIS do merge):

```text
production SHA            git rev-parse HEAD                 (hoje 56aef0c… + commits docs)
runtime hashes            tabela "Snapshot do runtime implantado"
parâmetros do .env        tabela "Parâmetros em vigor no marco"
SKILL hash                3ad86dcebb0a25ce22c36868259043d4b1029e8b203c0a1f978c7e28b1cb46f5
monitor hash             8b8d72a78066d5f8ea9c5f0389bbaacbda84c84b9a2dec5f7f97d21563b382d3
monitor transformação    sed '@PROJECT_ROOT@' -> caminho absoluto (deve seguir conferindo por hash)
```
