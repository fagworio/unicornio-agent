# Política de Imagens (UnicornioHater)

> Referência completa consumida pelo SKILL `unicorniohater-editor` ANTES de
> montar o `media_plan`. A regra editorial é: **nenhuma imagem errada é melhor
> que uma imagem** — falhe fechado na dúvida.

## Princípio

Crédito visível é obrigatório, mas **não concede permissão**. O pipeline aceita
somente CC0, domínio público, CC BY ou CC BY-SA sem variantes NC/ND, com a URL
oficial correspondente. Texto livre como “Uso com crédito” ou “permissão
concedida” não é uma licença aceita. Além de validar os campos, o upload exige
que a página original contenha Schema.org `ImageObject` com `contentUrl` igual
ao URL direto da imagem e `license` apontando para os mesmos termos oficiais
declarados. Uma licença genérica da página, uma declaração do agente ou a
licença de outra imagem não comprovam os direitos. Fontes sem essa evidência
estruturada são rejeitadas para upload; não contorne a rejeição nem preencha
licença por inferência. A verificação automatizada é evidência técnica da
declaração da origem, não parecer jurídico independente.

Provedores de busca são **somente índices de descoberta** — a fonte é a página
**ORIGINAL** da imagem (preview, thumbnail ou resultado do buscador não são
fonte de proveniência nem de direitos).

## Ordem de busca

1. **Reuso elegível primeiro**: consulte `media-search` e use apenas entradas
   do mesmo subject com proveniência e metadados de direitos completos e
   compatíveis. Registros legados sem licença explícita permanecem úteis para
   deduplicação, mas não podem ser reutilizados. Busque na web só para cobrir o
   déficit restante.
2. **Busca web configurada**: `media-search-web TERMO --size xga --ratio w
   --limit N` executa a ordem de `EDITOR_MEDIA_SEARCH_PROVIDERS` (por padrão:
   DuckDuckGo, Bing, Yandex e Google HTML). DuckDuckGo é um backend não oficial;
   pode falhar e não é presumido independente do Bing. Bing/Yandex formam o
   plano obrigatório padrão para `EXHAUSTED_VERIFIED`; DDG e Google HTML são
   oportunísticos. Nenhum provider dispensa a validação da página ORIGINAL.
   Não contorne CAPTCHA, consentimento ou bloqueios.
3. **Query como contexto**: registre `search_query` no media_plan (a busca
   que retornou a imagem). O gate de relevância usa esse contexto junto às
   evidências da imagem e da página; declare a query real, sem inventá-la para
   justificar uma imagem sem relação.
4. **Wikimedia Commons é FALLBACK** (pouca key art oficial e rate-limit 429):
   não trave re-tentando Wikimedia; troque para fontes web imediatamente.

## Regras obrigatórias (aplicadas deterministicamente no `apply`)

- **(a) Mínimo 2/4/6 SEMPRE**: 2 <= 600 palavras, 4 <= 1000, 6 acima;
  listicle = `max(2, nº itens)` — **1 item = 1 imagem real: dimensione o
  listicle pela disponibilidade real (4 imagens inline reais e distintas =
  no máximo 4 itens; a featured não entra na conta; nunca Top 10 sem 10 imagens)**.
  Post sem o mínimo **NÃO vira READY**.
- **(b) Toda imagem retrata EXATAMENTE a obra citada** — nunca conceito
  genérico (um morcego real NÃO vale para um jogo de vampiro; uma multidão de
  convenção NÃO vale para um anime específico).
- **(c) Featured = key art da obra citada**, nunca arte da matéria. O gate usa
  `source_only=True`: o nome real do arquivo/página de origem deve carregar a
  entidade da obra (o alt/credit que você escreve pode decorar uma imagem
  errada). NUNCA use como featured um **card de manchete / banner tipográfico /
  share-card de notícias do site** (texto sobre fundo, sem a arte da obra): o
  gate de visão (`require_key_art`) rejeita `text_banner`/`infographic` mesmo
  que o texto cite a obra. Wordmark/title-treatment OFICIAL da obra é aceitável;
  card de notícia SOBRE a obra não é.
- **(d) Inline 640–1280px** (largura); conteúdo publica com dimensões reais.
- **(e) URL direta listada na página de origem** — verificação byte-a-byte no
  apply (`verify_downloaded_against_source`).
- **(f) Crédito visível em toda imagem** (`Crédito da imagem: ...`).
- **(g) SEM imagem transparente**: o pipeline acha o canal alpha e achata
  sobre fundo branco antes do upload/insert (o WebP publicado nunca é
  transparente). Imagem (quase) totalmente transparente é **REJEITADA** —
  troque por uma versão com fundo.
- **(h) SEM imagem repetida no mesmo post**: cada imagem deve ser distinta.
  Não reutilize a mesma URL/imagem várias vezes no corpo do post (nem como
  featured + inline). O checklist `imagens_duplicadas` bloqueia duplicatas.

## O que registrar no `media_plan`

```json
{
  "paragraph_index": 2,
  "source_page_url": "https://pagina-original/",
  "direct_image_url": "https://cdn/pagina/header.jpg",
  "author": "Autor/Estudio",
  "license": "CC BY 4.0",
  "license_url": "https://creativecommons.org/licenses/by/4.0/",
  "captured_at": "2026-08-21",
  "credit_text": "Crédito da imagem: Autor. Obra. Licença CC BY 4.0.",
  "alt_text": "Obra key art",
  "is_featured": false
}
```

O exemplo só pode ser aplicado se a página de origem também vincular essa
licença ao `ImageObject` cujo `contentUrl` seja exatamente a imagem indicada.
O agente não deve inventar campos de licença para tentar passar a validação.

## Reuso da Media Library

`media-search` retorna candidatos do acervo. **Sem subject exato, página de
origem, licença compatível, URL oficial da licença, autoria, data de captura,
crédito e alt text explícitos, a imagem NÃO pode ser reutilizada**. Registros
legados incompletos continuam disponíveis para deduplicação, mas não para uso
editorial.
O reuso nunca edita o attachment original — baixa e re-envia como NOVO
attachment.

`media-search-web` já consulta o índice local/Media Library ANTES da web e
devolve o bloco `reuse` (URL original + página de origem + pHash + media_id):
use esses itens primeiro e só busque na web o déficit restante
(`capacity.missing`).

**`media_library_id` NÃO substitui a URL original.** No `media_plan` ponha
sempre `direct_image_url` = URL ORIGINAL listada na página de origem (o apply
rebaixa e confere byte a byte; a URL S3 do nosso upload não consta na página e é
rejeitada com "imagem baixada nao consta na pagina de origem").

## Posição no texto (`paragraph_index`)

- Imagens entram **APÓS** o parágrafo alvo; entre duas imagens exigem **>= 3
  parágrafos** de distância; índice máximo = `len(</p> do conteúdo) - 2`
  (senão: `media must be inserted between paragraphs`).
- Contagem de blocos: `content POST_ID` (só quando a reescrita for necessária) ou
  o `word_count` do card.
- A **featured não entra no insert** (vai como `featured_media`), então não ocupa
  vaga de spacing — mas o índice dela conta na validação do plano.
- Teto estrutural de imagens inline: `floor((blocos-1)/3)+1`. Post com 14 blocos
  aceita 5 imagens mesmo exigindo 6: **corrija o TEXTO** (divida 3 parágrafos
  longos em dois, mesmo texto) — jamais encurte o artigo para cair de faixa.
- **Listicle**: a imagem entra LOGO após o H2 numerado; a regra de 3 parágrafos
  não vale (`paragraph_index` = 1º parágrafo após cada H2 `N. Título: descrição`).

## Fallbacks

- Se após busca honesta não houver imagem real relevante, deixe o `media_plan`
  sem esse item e execute `apply`. Depois de `site_relevance=process`, o
  checklist classifica a falta como `PARTIAL`; não use `uncertain` nem force uma
  imagem errada.
- Imagem inline não-WebP relevante: o `apply` normaliza automaticamente para
  WebP (sem nova busca semântica) — não procure de novo.

## Waiver `media_exhausted` (busca esgotada)

Quando a busca de imagens foi HONESTA e `media-search-web` devolveu
`count=0` (não há imagem para baixar), declare `media_exhausted: true` no
JSON editorial (booleano opcional). O código então:

- **Artigo (não listicle)** com featured presente e válida: dispensa o mínimo
  de imagens inline (`imagens_no_corpo` + a parte de imagem do
  `qualidade_texto`) e publica com featured + texto. A featured continua
  obrigatória e validada (relevância + visão).
- **Listicle (Top N)**: NÃO dispensa — o post vai para `awaiting_human`
  (revisão manual sua), sem loop de rework (não queima token re-tentando).

Só declare `media_exhausted` após buscar de verdade; nunca para "pular" a
busca de imagens.

**Backstop determinístico (não depende do campo):** mesmo que você NÃO declare
`media_exhausted`, o código decide sozinho após `EDITOR_MAX_MEDIA_SEARCH_ATTEMPTS`
(default 2) applies falhando em imagens com featured presente: artigo waiva
inline (publica) e listicle vai para `awaiting_human`. Cada apply falho = 1
busca completa (o `media-search-web` já rotaciona Bing/Yandex internamente
numa só chamada, + fallback `web_search`). O `media_exhausted`
apenas adianta a decisão (economiza 1 ciclo).
