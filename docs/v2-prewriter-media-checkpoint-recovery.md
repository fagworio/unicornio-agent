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

## Contenção administrativa de um parcial legado sem sidecar

Quando a retomada automática não pode provar a correlação, o comando
administrativo existente v2-isolate-uncertain pode ser usado para conter um
alvo pré-Writer, sem transformá-lo em retry. O preview exige a identidade exata
da autorização e mantém o alvo como UNCERTAIN_ISOLATED. Para aplicar, exige
autorização administrativa explícita e prova completa dos demais posts da
rodada; qualquer outro post ainda incerto bloqueia o encerramento.

Para essa categoria, o preview registra os hashes e bytes originais do parcial,
a presença ou ausência explícita do sidecar, o estado do MediaRegistry e
fingerprints sanitizados dos attachments referenciados. URLs e outros valores
brutos de attachment não são incluídos no relatório. Antes de aplicar ou
retomar uma transição interrompida, os arquivos, fingerprints, journals,
Registry e estado WordPress são lidos novamente. Qualquer mudança invalida a
transição. Um sidecar ausente continua ausente no arquivo; o procedimento nunca
fabrica correlação.

O isolamento não executa POST/PUT/PATCH/DELETE, não faz upload, não altera o
post WordPress e não declara mídia aplicada, READY ou publicada. O journal e o
parcial originais são preservados. Só os posts individualmente comprovados
entram na contabilidade da rodada; o alvo isolado permanece em quarentena e
excluído de novos escritores até resolução independente.
