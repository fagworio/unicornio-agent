# Recuperação automática de rodada V2

O ciclo editorial pode recuperar uma autorização pendente quando a reconciliação
normal encontra efeitos cuja conclusão não pode ser comprovada. A recuperação
classifica novamente **todos** os posts da rodada pelo mesmo coletor de
evidências usado pelas ferramentas administrativas (`_round_post_evidence`).

## Garantias e limites

- A recuperação só começa enquanto o lock global V2 e os locks de **todos** os
  posts selecionados estiverem adquiridos e ainda pertencendo à execução.
- Cada post precisa de readback WordPress válido e evidência de um MediaRegistry
  presente, íntegro e com o schema necessário. Evidência incompleta ou identidade
  divergente mantém o ciclo bloqueado.
- `COMMITTED_NO_REPLAY`, `NOT_EXECUTED` e `PRE_WRITER_RECOVERABLE` são tratados
  conforme a prova individual. Somente `UNCERTAIN_ISOLATED` é acrescentado à
  quarentena.
- Antes da transição são arquivados checkpoint, journal do ciclo, journals dos
  posts, artefatos de mídia encontrados e uma cópia consistente do Registry,
  com hashes verificáveis.
- A transição altera somente arquivos locais de checkpoint, journal e política
  de quarentena. Não chama WordPress para escrita, não executa Writer, upload,
  aplicação de mídia ou Publisher.
- O ciclo encerra a invocação após a transição; a nova seleção ocorre numa
  invocação posterior, sem contabilizar a rodada interrompida como executada.

Se qualquer lock estiver ocupado, o Registry/readback não puder ser validado,
uma classificação não cobrir exatamente a seleção, ou o arquivo de transição
divergir, a recuperação falha fechada e preserva o ciclo para diagnóstico.

## Interrupções e retomada

A transição local é versionada e idempotente. Estados preparados são retomados
sob os mesmos locks antes de o cron ler o checkpoint. Uma intenção automática
interrompida é reavaliada contra as evidências atuais antes de continuar; uma
intenção iniciada por operador não é promovida pelo cron e exige retomada
administrativa explícita.

Esta automação não substitui a auditoria operacional de um checkpoint real nem
autoriza `--fresh`, replay manual, alteração de journals ou deploy em produção.
Produção deve ser validada separadamente, incluindo a confirmação da release
instalada e a observação da execução natural.
