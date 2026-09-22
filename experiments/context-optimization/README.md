# Experimento de otimização de contexto (branch `next/context-optimization`)

Aparato de COMPARAÇÃO para responder à pergunta original:

> por que um post consumia contexto/tokens demais?

Este diretório **não implementa nenhuma otimização** e **não é importado pelo runtime**
(nada aqui está em `src/`, nada é chamado pelo `unicornio-editor`, nada é instalado no
ambiente Python do cron). Ele existe para que, quando a primeira sessão cron VÁLIDA
aparecer, a decisão de onde otimizar saia dos dados — e não de nova hipótese.

Regras do congelamento em vigor (ver `hermes/references/congelamento-experimento.md` na
`main`): produção congelada, coleta ARMADA, nenhum merge/deploy enquanto a coleta estiver
ARMADA ou ATIVA. Trabalho aqui é preparação de alternativas.

## Invariantes deste aparato

1. **Stdlib apenas**, `python3` do sistema; nenhum arquivo daqui importa `src/unicornio_editor`
   (as ferramentas leem dados e, quando precisarem de um gate do repo, importam o pacote
   apenas no venv do worktree `next` — nunca no ambiente do cron).
2. **Somente leitura** nas duas fontes reais: `work/telemetry.jsonl` do projeto e
   `state.db` do Hermes (aberto com `file:...?mode=ro`).
3. **Nenhuma escrita em produção**: nada em `/www/wwwroot/hermes/unicornio-agent`, nada em
   `~/.hermes` (skills, scripts, cron, state.db).
4. **Nenhum comando do pipeline é executado** para gerar dados de comparação: isso criaria
   eventos de telemetria e mexeria na fila editorial. Os dados vêm de execuções reais já
   encerradas.
5. **Proveniência obrigatória**: toda fixture carrega sessão, id de mensagem, ts, bytes,
   sha256 e nº de redações no cabeçalho + `index.json`.
6. **Nada sintético apresentado como real**: se um exemplo for inventado para teste de
   parser, ele vai marcado como `SINTETICO` no nome e no conteúdo.

## Layout

```text
experiments/context-optimization/
├── README.md                    (este arquivo)
├── payload-tools.md             (especificação do Experimento A)
├── hermes-session-replay.md     (especificação do Experimento B)
├── fixtures/                    (payloads REAIS capturados do state.db + índice)
├── results/                     (saídas reais das ferramentas, com rótulo da fatia)
└── tools/                       (ferramentas read-only de medição)
    ├── telemetry_payload_inventory.py   A: bytes por comando
    ├── session_replay.py                B: custo por sessão + decomposição
    ├── ab_criterion.py                  A x B: os dois cruzados + limiares
    ├── capture_payloads.py              fixtures reais (com redação)
    ├── probe_sources.py                 auditoria de esquema das fontes
    └── probe_flags.py                   content vs api_content e flags de mensagem
```

## Fontes de dados (definições que importam)

| Fonte | Campo | O que é |
|---|---|---|
| `work/telemetry.jsonl` | evento `cmd_output` | `bytes` = tamanho do JSON *pretty-printed* que o comando imprimiu (o texto que o LLM consome), `command`, `kind` (read/write), e `run_source`/`cron_job_id`/`session_id` quando instrumentado |
| `state.db` `sessions` | `api_call_count`, `input_tokens`, `cache_read_tokens`, `cache_write_tokens` | prompt real = `input + cache_read + cache_write` (mesma definição de `session_metrics.py`); `input_tokens` sozinho subestima |
| `state.db` `session_model_usage` | `task != ''` | chamadas AUXILIARES (aprovação, visão, compressão, título) — invisíveis na tabela `sessions` |
| `state.db` `messages` | `role`/`content`/`tool_name` | o texto real que o agente viu; `token_count` está vazio nesta base (use bytes) e `api_content` está NULL (ou seja: `content` é o que foi enviado) |
| `state.db` `system_prompts` | `LENGTH(prompt)` via `sessions.system_prompt_hash` | custo FIXO por request (31.374 B nas sessões do cron) |

## Como rodar

```bash
cd /www/wwwroot/hermes/unicornio-agent-next/experiments/context-optimization

# A: inventário de bytes por comando
python3 tools/telemetry_payload_inventory.py                 # arquivo inteiro (fatia contaminada)
python3 tools/telemetry_payload_inventory.py --hours 720     # últimos 30 dias

# B: replay de sessões já encerradas (exato + decomposto)
python3 tools/session_replay.py --limit 3

# A x B: recomendação com limiares declarados (fatia oficial = cron)
python3 tools/ab_criterion.py --hours 24
python3 tools/ab_criterion.py --hours 72 --include-historical   # referência contaminada

# fixtures reais de uma sessão (com redação de credenciais)
python3 tools/capture_payloads.py --session cron_9e39343dc6f5_20260922_064523 --min-bytes 500
```

## Pitfalls já encontrados (não repetir)

1. **Payload real tem espaço no fim de linha.** As fixtures são stdout de verdade e várias
   linhas terminam com espaço; `git diff --check` reprova e o step "Check whitespace" do CI
   falharia no merge. Normalizar as fixtures seria falsificar a evidência (os bytes são o
   que se mede). Solução adotada: `.gitattributes` desliga o check de whitespace **apenas**
   em `experiments/context-optimization/fixtures/**` — está documentado no próprio arquivo.
2. **Saída de ferramenta não pode terminar com linha vazia.** Um `print()` final em
   `session_replay.py` gerava `new blank line at EOF` nos arquivos de `results/` e reprovava
   o mesmo step. Corrigido na ferramenta (regenerar a rodada, não editar o arquivo).
3. **`git diff --check origin/main...HEAD` só vale depois do commit**: ele compara commits,
   não o índice. Conferir antes de commitar dá falso verde.
4. **`bytes` da telemetria ≠ tamanho do payload que você tem em mãos.** `cmd_output.bytes`
   é o JSON *pretty-printed* que o comando imprimiu; a fixture é o texto que o agente
   recebeu. Cruzar as duas fontes, nunca tratar uma como a outra.
5. **`input_tokens` sozinho engana.** O prompt real é `input + cache_read + cache_write`;
   `cache_read` é ~98-99% dele. Qualquer conta que ignore isso subestima em ~100x.
6. **`messages.token_count` está vazio** nesta base: crescimento por request é proxy em
   bytes. Declare o proxy; não o apresente como token medido.

## Primeiros números reais (pré-congelamento, só como referência)

Três sessões de cron instrumentadas por sessão (não é a amostra oficial: o marco oficial
só começou em 2026-09-22T15:54:51Z e ainda não houve sessão válida):

| sessão | requests | prompt tokens | cache_read | peso tools stdout | peso abertura (SKILL) | peso prompt de sistema |
|---|---|---|---|---|---|---|
| `..._20260922_042722` | 97 | 14,057,558 | 98.9% | 83.6% | 5.9% | 10.3% |
| `..._20260922_064523` | 136 | 20,417,371 | 99.4% | 79.9% | 7.4% | 12.8% |
| `..._20260922_090524` | 14 | 494,408 | 97.9% | 21.3% | 30.7% | 47.7% |

Leitura: **o custo é reenvio do mesmo contexto** (cache_read ~98-99% do prompt) e, dentro
dele, o **stdout das tools é a maior massa identificada** (80-84% do peso nas sessões
longas). Em sessão curta o custo fixo (prompt de sistema + SKILL de abertura) domina —
o que muda a conclusão conforme o tamanho da sessão, e é exatamente por isso que a decisão
precisa da sessão válida da amostra oficial.

## Estado da decisão

```text
amostra oficial (run_source=cron, job 9e39343dc6f5)   = VAZIA (experimento ARMADO)
recomendacao provisoria (fatia contaminada)           = A primeiro (payload de tools)
status                                                = nenhuma conclusão, nenhum deploy
```

`tools/ab_criterion.py` imprime "AMOSTRA OFICIAL VAZIA" e **não recomenda** enquanto não
houver evento `run_source=cron` na janela — a recomendação acima veio da fatia histórica e
serve só para mostrar que o aparato funciona.
