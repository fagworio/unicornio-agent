# V2: retomada segura de checkpoint de mídia pré-Writer

O estado `writer_started=false` não prova ausência de uploads. A aquisição
pode ter confirmado attachments antes de o Writer começar. O V2 agora distingue
um checkpoint de aquisição correlacionado de efeitos de mídia ainda incertos.

## Contrato de retomada

Cada gravação de `backups/<post_id>/editorial.partial.json` também grava
`editorial.partial.v2.json`, contendo a identidade da execução, os IDs de mídia
e o SHA-256 exato do checkpoint. O ciclo só prepara uma retomada quando:

- o journal V2 confirma `writer_started=false` e corresponde à mesma execução;
- o sidecar, o checkpoint, `media_runs` e `media_operations` têm identidade
  consistente, e o Registry V5 está estruturalmente completo;
- a operação registrada continua sem write, sem readback e sem conclusão;
- o post WordPress continua `pending`, e seu estado de ciclo de vida permanece
  igual ao estado do journal;
- cada attachment do checkpoint existe no WordPress com o mesmo ID e URL, e
  nenhuma mídia nova do checkpoint já aparece no conteúdo ou como featured.

Com essa evidência, o journal é marcado como `interrupted_pre_writer` e
retomável. A próxima tentativa reutiliza os IDs preservados e passa novamente
pelos gates normais de mídia, conteúdo, Writer e READY. A recuperação não marca
mídia como `applied`, não altera o post e não publica.

## Limites

Sidecar ausente, hash divergente, identidade parcial, Registry incompleto,
readback indisponível ou qualquer indício de aplicação mantêm o bloqueio
`MEDIA_EFFECTS_PRE_WRITER`. Checkpoints criados antes desse protocolo não se
tornam recuperáveis automaticamente; exigem reconciliação explícita com as
evidências históricas existentes. O mecanismo não altera limites de tentativas
nem isola automaticamente uma autorização arbitrária com efeitos incertos.
