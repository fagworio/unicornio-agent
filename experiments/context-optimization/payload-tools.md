# Experimento A — Tool Payload

Objetivo: saber **quanto do stdout dos comandos é necessário** para a próxima decisão do
agente, e qual versão mais compacta preserva essa decisão. Nada aqui muda o CLI: os
candidatos são medidos **offline**, contra payloads reais já entregues.

## Perguntas que A precisa responder

1. Qual comando devolve mais bytes?
2. Quantas vezes ele é chamado por READY?
3. Quanto do stdout é necessário para a próxima decisão?
4. Quanto está sendo repetido entre chamadas?

As perguntas 1, 2 e 4 já têm resposta medida por ferramenta (abaixo). A 3 é a que exige
comparar o payload real com um candidato compacto — e é onde este experimento gasta o
esforço.

## Insumo: fixtures reais

```bash
python3 tools/capture_payloads.py --session cron_9e39343dc6f5_20260922_064523 --min-bytes 500
```

107 entregas, 318.447 B, extraídas das mensagens `role='tool'` do `state.db` (o texto que
o agente realmente viu), com cabeçalho de proveniência (sessão, id, ts, bytes, sha256,
redações). `fixtures/<sessão>/index.json` é o índice.

## Medição atual (fatia: arquivo inteiro do `work/telemetry.jsonl`, inclui execução manual)

```text
comando                eventos     total B      %    media      p50      p90       max  por READY
content                    514     3812238  19.7%     7417     6189    12905     33867      18067
queue                      449     3683455  19.1%     8204     7258    17455     22229      17457
cards                      541     2813378  14.6%     5200     3830    11497     44264      13334
telemetry                   94     2716437  14.1%    28898     1821   105208    120109      12874
media-search-web          1016     2497183  12.9%     2458     2184     3927     25620      11835
media-search              1004     1772951   9.2%     1766     1153     3950     16392       8403
draft                      105     1558360   8.1%    14842    14009    23771     25850       7386
list-pending               106      295332   1.5%     2786     1319     1354     83793       1400
media-search-listicle       21      119052   0.6%     5669     5573     9802     11342        564
prepare                     66       34512   0.2%      523      316      366     13838        164
checklist                   11       20606   0.1%     1873      622     4046      4103         98
media-validate               1         559   0.0%      559      559       559       559          3
```

Leitura: não há um único vilão em volume total — há três perfis distintos:

| perfil | comandos | o que isso pede |
|---|---|---|
| muito frequente, payload médio | `content` (514x, 7.4 KB), `queue` (449x, 8.2 KB), `cards` (541x, 5.2 KB) | corte por CAMPO e por cardinalidade de lista; cada KB economizado se multiplica por dezenas de chamadas |
| pouco frequente, payload gigante | `telemetry` (94x, 28.9 KB de média, p90 105 KB), `draft` (105x, 14.8 KB) | variante `--compact`/`--summary`; a cauda (p90/max) é o alvo, não a média |
| ruído | `prepare`, `checklist`, `media-validate`, `list-pending` | não vale otimização |

`cards`/`queue` chamados ~500x cada com apenas 211 READY na base mostra (pergunta 4) que há
**repetição de leitura**: o mesmo estado é reconsultado muitas vezes. A variante compacta
precisa, portanto, ser medida junto com o número de chamadas — não só em bytes por chamada.

## Candidato e métricas

Para cada comando e cada ideia de compactação, produzir:

```json
{
  "comando": "cards",
  "fixture": "fixtures/.../86023_terminal.txt",
  "original_bytes": 2353,
  "candidate_bytes": 1180,
  "reduction_pct": 49.9,
  "fields_removed": ["..."],
  "decision_equivalence": {"original": "...", "candidate": "...", "equal": true},
  "gates": {"preservados": 4, "perdidos": 0},
  "extra_calls_required": 0,
  "aceito": true
}
```

Regra de validade (as duas juntas, sem exceção):

```text
mesma próxima decisão do agente
+  mesmos hard gates disponíveis (nenhum gate perde o que precisa ver)
=  candidato válido
```

Reduzir JSON não é evidência de nada: um payload 70% menor que obrigue uma segunda
chamada para recuperar o campo perdido **aumenta** o custo. `extra_calls_required > 0`
com `reduction_pct` alto é reprovação, não sucesso.

### Como provar "mesma próxima decisão" offline

1. **Inventário de campos que decidem** por comando (derivado do SKILL + dos gates do
   repo): `cards` → `id`, `state`, `title`, `last_error`/motivo, contadores; `content` →
   índices de parágrafo e o texto que o draft consome; `media-search-*` → `accepted`,
   `rejected` (motivo), `source_page`, `license`, dimensões; `draft` → `site_relevance`
   (decision/confidence), `media_plan` (source_page/license/alt), contagens de gate;
   `apply` → `failure_reasons`.
2. **Corte por campo**: o candidato preserva esses campos byte a byte e remove só o resto.
3. **Verificação pelos gates do próprio repo** (importando o pacote no venv do `next`,
   nunca no ambiente do cron): rodar as funções de gate que consomem o payload sobre o
   ORIGINAL e sobre o CANDIDATO e exigir o MESMO veredito. Gate que muda de veredito
   reprova o candidato, mesmo que a decisão do agente pareça igual.
4. **Sem tocar produção**: nenhuma chamada `apply`/`media-validate` real, nenhum evento de
   telemetria gerado, nenhuma alteração na fila.

### Alvos concretos (por perfil, com o número que motiva cada um)

| comando | massa medida | candidato a medir | risco principal |
|---|---|---|---|
| `telemetry` | 14.1% do total; p90 105 KB | variante resumida (contadores + KPIs), sem séries cruas | perder a série usada para diagnóstico |
| `content` | 19.7%; 514 chamadas | paginação por parágrafo + contagem, em vez do `cleaned_html` inteiro | o draft perde contexto e faz segunda chamada |
| `queue`/`cards` | 33.7% juntos; ~1000 chamadas | corte de campos redundantes + teto de lista (`unprocessed_ids`) | esconder `state`/motivo de bloqueio |
| `draft` | 8.1%; média 14.8 KB | só os campos que o próximo passo lê + caminho do JSON em disco | gate perder `media_plan` |
| `media-search-web` | 12.9%; 1016 chamadas | manter aceitos + motivos agregados de rejeição | perder `source_page` (proveniência é HARD GATE) |

## Registro de resultado

```bash
# saída de cada rodada vai para results/, com o rótulo da fatia
python3 tools/telemetry_payload_inventory.py --hours 720 > results/$(date -u +%Y-%m-%dT%H%MZ)_payload-inventory_30d.txt
```

O resultado de um candidato é o JSON do schema acima em
`results/<ts>_payload-candidates_<comando>.json`, com o comando que o gerou anotado no
`results/README.md`.

## O que invalida um candidato

- qualquer gate do repo que mude de veredito entre original e candidato;
- próxima decisão diferente (mesmo que "melhor");
- exigir chamada extra para recuperar campo removido;
- payload reduzido que precise de pós-processamento pelo agente (isso é custo escondido);
- `source_page`/proveniência ausente (HARD GATE do projeto, não é negociável).

## Estado

Nenhum candidato implementado e nenhum medido ainda: a medição por campo depende de a
primeira sessão VÁLIDA (amostra oficial) definir o alvo vencedor — A ou B (ver
`hermes-session-replay.md` e `tools/ab_criterion.py`). As fixtures reais já estão
capturadas e o inventário de bytes por comando já está medido (tabela acima), então o
trabalho pode começar no dia em que a amostra apontar A.
