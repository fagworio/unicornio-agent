# V2 — encerramento seguro de rodada e isolamento múltiplo

Esta alteração não executa deploy nem transição administrativa. Ela adiciona uma
transição versionada para encerrar uma autorização parcial depois de um preview
read-only, sem replay de WordPress.

## Auditoria da autorização `4a2256fc0b744d45b8125139c197fee6` / round 1

A auditoria de produção encontrou:

- `115215`: journal histórico `committed`, `run_id=214048e59875449ea0011265d411873d`;
  três `media_runs` e decisões são anteriores à autorização atual, mas as duas
  linhas de `media_usage` têm `applied_at=NULL`.
- `115512`: journal histórico `committed`, `run_id=6edd8d23f79944c2a8e1c4ac5b9e6153`;
  quatro `media_runs` e decisões são anteriores à autorização atual, mas a linha
  de `media_usage` tem `applied_at=NULL`.
- Os journals históricos não têm `cycle_id`, `cycle_round` ou `authorization_id`
  da autorização atual.

Portanto, a ausência de timestamp em `media_usage` impede provar que não houve
efeito da autorização corrente. Timestamps inválidos ou ausentes permanecem
`unknown`; nunca são convertidos em prova de ausência. Timestamps ISO-8601 com
fuso são normalizados para UTC antes da comparação. Strings não são comparadas
lexicograficamente.

## Classificação esperada

- `115524`: `COMMITTED_NO_REPLAY`; reconciliado sem novo write e contado uma vez.
- `115331`: `UNCERTAIN_ISOLATED`; journal original preservado, sem `READY` e sem publicação.
- `115215`: `UNCERTAIN_ISOLATED`; ausência de `applied_at` impede `NOT_EXECUTED`.
- `115512`: `UNCERTAIN_ISOLATED`; ausência de `applied_at` impede `NOT_EXECUTED`.
- `115338`: `NOT_EXECUTED`, conforme a evidência já confirmada; sem autorização de publicação.

A contabilidade da rodada registra somente `115524` como tentativa executada,
fecha a rodada uma vez e define o próximo número como `2`. Posts isolados não
entram em `attempts_by_post`, não recebem `READY` e não são reexecutados.

## Procedimento separado

1. Fazer backup verificável e confirmar que o checkout ativo é exatamente a
   release aprovada. Não usar `--fresh`, reset, limpeza ou edição manual de
   checkpoint/journal.
2. Executar preview read-only no checkout operacional:

   ```text
   python -m unicornio_editor.cli v2-isolate-uncertain 115331 \
     --root /www/wwwroot/hermes/unicornio-agent \
     --cycle-id 4a2256fc0b744d45b8125139c197fee6 \
     --authorization-id a4371791eb824734b4b4c88ae0334a71 \
     --pending-round 1 \
     --selected-ids 115524,115331,115215,115512,115338 \
     --run-id 5e42e98f2d81423f90a66e8adbbaa8b8 \
     --isolated-post-ids 115331,115215,115512 \
     --run-ids 115331=5e42e98f2d81423f90a66e8adbbaa8b8,115215=214048e59875449ea0011265d411873d,115512=6edd8d23f79944c2a8e1c4ac5b9e6153
   ```

3. Confer o preview inteiro: identidade, hashes, classificação por post,
   `wordpress_writes=0`, `write_replayed=false` e `115524` como único item
   executado. Se qualquer hash, post, journal, MediaRegistry ou readback mudar,
   abortar.
4. Somente com autorização administrativa expressa, reapresentar o mesmo
   comando com `--preview-evidence <arquivo>` e `--apply --operator-authorization
   <autorização>`. A aplicação é idempotente e não escreve no WordPress.
5. Após a aplicação, ler de volta checkpoint, journal agregado, journals
   arquivados, quarentena, evento administrativo e `round_reports`. Confirmar
   `pending_round=null`, `rounds=1`, `attempts_by_post={115524: 1}`, ausência de
   replay e quarentena dos três posts incertos.
6. Não iniciar execução manual adicional. Validar somente a execução natural do
   cron oficial e o próximo número de rodada.
