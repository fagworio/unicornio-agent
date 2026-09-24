# Arquitetura

```text
Hermes cron (fresh session)
  -> SKILL.md
  -> cards --compact
  -> prepare-batch (até 2 posts novos; envelopes independentes)
       -> snapshot
       -> remove imagens antigas
       -> unwrap article/div
       -> limpar CTA/Fonte antigos
  -> editorial-generate-batch (uma chamada HTTP estruturada, sem loop Hermes,
     saída keyed por post_id; needs_retry isolado)
       -> texto revisado
       -> SEO Rank Math
       -> media_plan
       -> trailer plan
  -> media-resolve-batch (reuso + descoberta + origem + relevância + pHash)
  -> vision-batch (opcional; somente candidatos ambíguos já filtrados)
  -> confirmar licença na página original + guardar evidência
  -> download temporário -> validação -> WebP
  -> POST /wp/v2/media (Media Library local, sem bucket/CDN)
  -> crédito de imagem obrigatório
  -> apply-batch (orquestrador; apply individual por post)
  -> builder
       -> insere imagens por paragrafo
       -> insere trailer
       -> CTA canonico
       -> Fonte de original_link, somente quando preenchido
  -> validator
       -> HTML
       -> CTA/Fonte
       -> integridade de tamanho
       -> alt/src
  -> re-fetch status
       -> se != pending: ABORTA
       -> se pending: update content/meta/featured_media sem campo status
```

O batch é apenas uma camada de transporte/orquestração. Backup, lock, checklist,
manifest SHA-256, `_hermes_state`, rework e falhas continuam isolados por post;
não existe transação multi-post no WordPress.

No happy path, o Hermes deve emitir um único editorial batch para os dois posts
e entregar as etapas determinísticas diretamente ao Python. A meta operacional
é uma inferência editorial, zero inferências para mídia/apply e no máximo uma
requisição multimodal `low` (mais uma `high` apenas para ambíguos).

`editorial-generate-batch` torna essa meta uma propriedade do código: recebe
`editorial.input.json`, faz exatamente uma chamada ao provider configurado por
`EDITORIAL_BASE_URL`/`EDITORIAL_MODEL` e grava `editorial.output.json`. O Hermes
coordena as etapas, mas não controla o loop da inferência editorial.

## Política de imagens e direitos autorais
O Google Images será usado somente para descobrir candidatos. Ele indexa imagens de terceiros e não concede licença. O agente só pode selecionar imagens cujo domínio público, licença Creative Commons compatível ou permissão explícita possa ser confirmada na página original.

Para cada imagem aprovada, o `media_plan` deve registrar `source_page_url`, `direct_image_url`, `author`, `license`, `license_url`, `captured_at` e o texto de crédito. A cópia será baixada, convertida para WebP e enviada à Media Library do WordPress; não haverá bucket ou CDN externo. O crédito visível deve seguir os termos da licença. Se a licença não for verificável, a imagem será recusada. Crédito isolado não transforma uma imagem protegida em autorizada.

## Por que não existe plugin auxiliar
O projeto usa a REST API existente e não instala plugin auxiliar. Rank Math continua sendo a integração de SEO; a mídia será armazenada localmente na Media Library, sem dependência do WP Offload Media.

## Concorrencia
`prepare` cria lock local por post com TTL. Antes de gravar, `update_post` consulta novamente o status. Isso evita editar um post que tenha sido publicado/reclassificado por outra pessoa enquanto o agente trabalhava.

O ledger de orçamento é particionado por `HERMES_SESSION_ID` em
`work/sessions/<session_id>.json`. Execuções Hermes distintas não herdam posts
tocados nem bytes de contexto; execuções locais sem esse id mantêm o arquivo
compatível `work/session_state.json`.
