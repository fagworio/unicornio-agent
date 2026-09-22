# Experimento B — Hermes Session / Replay

Objetivo: medir o outro lado da conta. Se o stdout das tools é pequeno e mesmo assim o
prompt por request é enorme, o problema **não** está no JSON do Unicornio: está na sessão
do Hermes (histórico reenviado, compressão, número de requests, custo fixo por request).

## O modelo de custo (é isso que muda a leitura)

Cada request envia o contexto INTEIRO de novo. Logo o custo de uma mensagem não é o
tamanho do seu texto: é

```text
custo_da_mensagem = bytes_da_mensagem x numero_de_requests_seguintes
```

Somando isso sobre todas as mensagens obtém-se o **peso** de cada componente. Foi o que
mostrou por que "cortar 2 KB de JSON" pode ser grande (a mensagem é reenviada dezenas de
vezes) e por que o prompt de sistema e a SKILL de abertura — que ninguém conta — somam
10-30% do custo.

Métricas por sessão (todas medidas, não estimadas, exceto onde marcado):

| métrica | definição |
|---|---|
| `requests` | `sessions.api_call_count` (main-loop) + chamadas auxiliares |
| `prompt_tokens` | `input + cache_read + cache_write` da tabela `sessions` (+ `session_model_usage` com `task != ''`) |
| `cache_read_share` | `cache_read / prompt_tokens` — fração do prompt que é reenvio do MESMO contexto |
| `prompt_por_request` | `prompt_tokens / requests` — tamanho médio do contexto por request |
| `peso_tools` | `Σ bytes(tool msg) x reenvios` |
| `peso_abertura` | bytes da 1ª mensagem `user` (SKILL + references injetadas) `x requests` |
| `peso_prompt_sistema` | `LENGTH(system_prompts.prompt)` `x requests` (custo fixo por request) |
| `residuo_tokens_por_request` | `prompt_por_request − peso_total / densidade_assumida` — o que o texto visível NÃO explica |

## Números reais (três sessões de cron já encerradas, pré-congelamento)

| sessão | requests | prompt tokens | cache_read | tools stdout | abertura (SKILL) | prompt de sistema |
|---|---|---|---|---|---|---|
| `..._20260922_042722` | 97 | 14,057,558 | 98.9% | 83.6% | 5.9% | 10.3% |
| `..._20260922_064523` | 136 | 20,417,371 | 99.4% | 79.9% | 7.4% | 12.8% |
| `..._20260922_090524` | 14 | 494,408 | 97.9% | 21.3% | 30.7% | 47.7% |

Comando: `python3 tools/session_replay.py --limit 3` (saída completa em
`results/2026-09-22T1718Z_session-replay_cron-3-sessoes.txt`).

Três fatos que mudam a conversa:

1. **O custo é reenvio.** `cache_read` é 97.9-99.4% do prompt: praticamente todo token
   pago é contexto já visto, reenviado.
2. **O tamanho da sessão decide quem domina.** Em 136 requests, o stdout das tools é
   79.9% do peso; em 14 requests, o custo FIXO (prompt de sistema 47.7% + SKILL de
   abertura 30.7%) é que manda. Otimização de payload não move uma sessão curta, e
   otimização de sessão não substitui payload numa sessão longa.
3. **A abertura é cara e invisível.** A 1ª mensagem `user` traz SKILL + references
   (~18-20 KB) e é reenviada em TODOS os requests: 2.46 M B-ponderados em 136 requests
   (7.4% do peso). Isso não é "payload de tool"; é custo de injeção de skill por sessão.

## O que ainda NÃO está medido (item de medição, não de otimização)

`residuo_tokens_por_request` varia de 9% a 50% do prompt conforme a densidade assumida
(2.0 a 3.5 bytes/token). Ou seja: **a densidade real (bytes/token) do conteúdo JSON não
está medida**, e sem ela a fração do prompt que é texto visível é desconhecida.

Sensibilidade medida (`tools/ab_criterion.py`, janela 72h):

```text
2.0 bytes/token -> 11.7% do prompt não explicado
2.5             -> 29.4%
3.0             -> 41.1%
3.5             -> 49.5%
```

Como fechar isso na rodada 2 (nenhuma das opções toca produção):

- **densidade por tipo de conteúdo**: tokenizar as fixtures reais (payload de tool,
  prosa, JSON de mídia) com um tokenizer proxy no venv do `next` (`tiktoken`/`cl100k`) —
  é proxy, não o tokenizer do modelo; declarar como proxy;
- **medição controlada**: uma chamada de teste no ambiente de dev, com texto de tamanho
  conhecido e contagem conhecida, para calibrar bytes/token do provedor em uso;
- **payload extra da requisição**: verificar se o request carrega o que não está no
  `state.db` (esquemas de tools, por exemplo). Enquanto não se sabe, o resíduo tem dois
  candidatos e nenhuma conclusão.

## Hipóteses de B, cada uma com o teste que a falsifica

| hipótese | teste offline | métrica decisiva |
|---|---|---|
| histórico longo domina | reconstruir o custo por request e ver a curva (`tools/session_replay.py`) | `prompt_por_request` crescente, `compactadas=0` |
| abertura da SKILL por sessão é custo fixo relevante | `peso_abertura / peso_total` | share > 5% justifica avaliar carregamento sob demanda de references |
| número de requests é o multiplicador | `requests x contexto médio` vs `prompt_tokens` | `requests_per_ready` (aparece só com a amostra oficial) |
| compressão não está atuando | `messages.compacted` = 0 em 301 mensagens | qualquer `compacted > 0` muda a leitura |
| chamadas auxiliares são irrelevantes | `session_model_usage` com `task != ''` | 20 chamadas de `approval` = 12.551 tokens de 20.417.371 (0.06%): hoje, irrelevante |

## Saída e registro

`tools/session_replay.py --json` emite o objeto completo (exato + decomposto +
calibração). Rodadas ficam em `results/<ts>_session-replay_<rotulo>.txt`.

## Estado

Nenhuma mudança de comportamento proposta: como o Experimento A, B só vira código depois
que a primeira sessão VÁLIDA da amostra oficial (`run_source=cron`, job
`9e39343dc6f5`) mostrar qual dos dois lados domina o KPI oficial. Até lá, o que existe é
medição — e duas candidatas a alvo dentro do próprio B: (i) o custo de abertura por
sessão (SKILL + references) e (ii) o multiplicador de requests por READY.
