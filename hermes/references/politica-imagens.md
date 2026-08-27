# Política de Imagens (UnicornioHater)

> Referência completa consumida pelo SKILL `unicorniohater-editor` ANTES de
> montar o `media_plan`. A regra editorial é: **nenhuma imagem errada é melhor
> que uma imagem** — falhe fechado na dúvida.

## Princípio (2026-08)

Qualquer imagem da web pode ser usada **com CRÉDITO VISÍVEL**. O crédito é a
evidência. Licenças livres (CC0, CC BY, domínio público, permissão) são
preferidas; para as demais, usar o marcador **"Uso com crédito"**.

O Google Images é **somente índice de descoberta** — a fonte é a página
**ORIGINAL** da imagem (o preview do Google não é fonte).

## Ordem de busca (regra 2026-08-21)

1. **KEY ART CACHE FIRST**: `work/keyart_cache.json` (obra -> imagens
   verificadas) e `unicornio-editor media-search TERMO --limit N` (reuso da
   Media Library). Busque na web só se nenhum dos dois servir. Imagem nova
   verificada -> REGISTRE no cache.
2. **Google Images com filtro do editor** (determinístico):
   `unicornio-editor media-search-web TERMO --size xga --ratio w --limit N`.
   Aplica imgsz=xga (1024x768) e imgar=w (proporção larga) + udm=2, e devolve
   candidatos com URL direta + página de origem + a `query` usada — sem você
   montar URL de busca nem parsear resultados (economia de tokens). O Google
   é apenas ÍNDICE: a fonte é a página ORIGINAL. Use `license: "Uso com
   crédito"` com `license_url` = a página original.
3. **Query como evidência**: registre `search_query` no media_plan (a busca
   que retornou a imagem). O gate de relevância aceita quando a query contém
   a obra — espelha o fluxo manual do editor. Declare a query real; nunca
   invente uma para decorar imagem errada (URL de origem ainda exigida).
4. **Wikimedia Commons é FALLBACK** (pouca key art oficial e rate-limit 429):
   não trave re-tentando Wikimedia; troque para fontes web imediatamente.

## Regras obrigatórias (aplicadas deterministicamente no `apply`)

- **(a) Mínimo 2/4/6 SEMPRE**: 2 <= 600 palavras, 4 <= 1000, 6 acima;
  listicle = `max(2, nº itens)`. Post sem o mínimo **NÃO vira READY**.
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
  "license": "Uso com crédito",
  "license_url": "https://pagina-original/",
  "captured_at": "2026-08-21",
  "credit_text": "Crédito da imagem: Autor. Obra. Uso com crédito.",
  "alt_text": "Obra key art",
  "is_featured": false
}
```

## Reuso da Media Library

`media-search` retorna candidatos com `tem_credito` (attachment carrega o
bloco "Crédito da imagem" no title/caption). **Sem crédito visível no
attachment, a imagem NÃO pode ser reutilizada** (falta evidência de licença).
O reuso nunca edita o attachment original — baixa e re-envia como NOVO
attachment.

## Fallbacks

- Se após busca honesta não houver imagem real relevante, use
  `unicornio-editor uncertain POST_ID --reason "..."` (decisão do agente) —
  NUNCA aplique sem imagens nem force uma imagem errada.
- Imagem inline não-WebP relevante: o `apply` normaliza automaticamente para
  WebP (sem nova busca semântica) — não procure de novo.
