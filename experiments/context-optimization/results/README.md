# results/ — saídas reais das ferramentas

Cada arquivo traz o cabeçalho da própria ferramenta (fonte, filtros, fatia). Este índice
registra o comando exato e o RÓTULO da fatia — sem isso um número "de 30 dias" vira
"baseline" por acidente.

| arquivo | comando | rótulo da fatia |
|---|---|---|
| `2026-09-22T1718Z_payload-inventory_fatiaz-todos.txt` | `python3 tools/telemetry_payload_inventory.py` | arquivo inteiro do `telemetry.jsonl`: **inclui execução manual** — referência histórica, não baseline |
| `2026-09-22T1718Z_payload-inventory_ultimos30d.txt` | `python3 tools/telemetry_payload_inventory.py --hours 720` | últimos 30 dias, mesma ressalva |
| `2026-09-22T1718Z_session-replay_cron-3-sessoes.txt` | `python3 tools/session_replay.py --limit 3` | 3 sessões do cron pré-congelamento: medição exata de tokens + decomposição em bytes×reenvios |
| `2026-09-22T1718Z_ab-criterion_24h.txt` | `python3 tools/ab_criterion.py --hours 24` | fatia oficial do cron vazia (0 eventos `run_source=cron`): **não recomenda** |
| `2026-09-22T1718Z_ab-criterion_72h-referencia.txt` | `python3 tools/ab_criterion.py --hours 72 --include-historical` | mesma janela com a fatia contaminada ao lado, explicitamente marcada como não oficial |

## Regras

1. Nome sempre com carimbo UTC (`YYYY-MM-DDTHHMMZ`) e rótulo da fatia.
2. Nunca editar um resultado à mão: reexecutar a ferramenta e versionar o novo arquivo.
   Se um número estava errado, o arquivo errado fica (histórico) e o novo explica a
   correção no `README.md` desta pasta.
3. Resultado de candidato do Experimento A entra como
   `results/<ts>_payload-candidates_<comando>.json` no schema de `payload-tools.md`.
4. Toda leitura de KPI oficial (`telemetry --sessions`) continua sendo feita no projeto
   (`unicornio-editor telemetry --sessions`), não aqui: esta pasta mede o mecanismo, não
   substitui o KPI.
