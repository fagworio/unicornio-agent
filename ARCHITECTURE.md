# Arquitetura

```text
Hermes cron (fresh session)
  -> SKILL.md
  -> list-pending
  -> prepare(post)
       -> snapshot
       -> remove imagens antigas
       -> unwrap article/div
       -> limpar CTA/Fonte antigos
  -> LLM editorial
       -> texto revisado
       -> SEO Rank Math
       -> media_plan
       -> trailer plan
  -> Google Images: descoberta de candidatos
  -> confirmar licença na página original + guardar evidência
  -> download temporário -> validação -> WebP
  -> POST /wp/v2/media (Media Library local, sem bucket/CDN)
  -> crédito de imagem obrigatório
  -> builder
       -> insere imagens por paragrafo
       -> insere trailer
       -> CTA canonico
       -> Fonte de original_link
  -> validator
       -> HTML
       -> CTA/Fonte
       -> integridade de tamanho
       -> alt/src
  -> re-fetch status
       -> se != pending: ABORTA
       -> se pending: update content/meta/featured_media sem campo status
```

## Política de imagens e direitos autorais
O Google Images será usado somente para descobrir candidatos. Ele indexa imagens de terceiros e não concede licença. O agente só pode selecionar imagens cujo domínio público, licença Creative Commons compatível ou permissão explícita possa ser confirmada na página original.

Para cada imagem aprovada, o `media_plan` deve registrar `source_page_url`, `direct_image_url`, `author`, `license`, `license_url`, `captured_at` e o texto de crédito. A cópia será baixada, convertida para WebP e enviada à Media Library do WordPress; não haverá bucket ou CDN externo. O crédito visível deve seguir os termos da licença. Se a licença não for verificável, a imagem será recusada. Crédito isolado não transforma uma imagem protegida em autorizada.

## Por que não existe plugin auxiliar
O projeto usa a REST API existente e não instala plugin auxiliar. Rank Math continua sendo a integração de SEO; a mídia será armazenada localmente na Media Library, sem dependência do WP Offload Media.

## Concorrencia
`prepare` cria lock local por post com TTL. Antes de gravar, `update_post` consulta novamente o status. Isso evita editar um post que tenha sido publicado/reclassificado por outra pessoa enquanto o agente trabalhava.
