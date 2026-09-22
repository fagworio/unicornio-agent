# Operação em Produção (UnicornioHater)

> Referência de pitfalls de produção consumida pelo SKILL `unicorniohater-editor`
> quando algo falha ou parece estranho. Consulte ANTES de explorar código.

## Interface (economia de tokens)

- O CLI é a interface. **NUNCA** leia `src/**`, `pyproject.toml`, `.env`,
  `backups/**`, logs ou JSONs grandes para "entender o fluxo". Exceção: erro
  não autoexplicativo -> leia SÓ a função do traceback.
- Para diagnosticar (fila, publicações, custo, crons, tokens): rode UMA chamada
  de `unicornio-editor queue --compact` / `list-pending --compact` (ou `scripts/diagnostico.sh`).
  Custo < $0.01 por verificação. **Não** explore state.db manualmente.

## Ambiente e execução

- O CLI lê env direto: sempre `set -a && . ./.env && set +a && .venv/bin/...`
  (sem o env ele cai na URL mock e "time out").
- `list-pending` consulta status=pending no servidor (filtro local retorna []).

## Janela de publicação (não confundir com cron quebrado)

- **JANELA DE PUBLICAÇÃO SILENCIOSA** = candidatos bloqueados pelo checklist
  (não é o cron quebrado). Diagnostique com `unicornio-editor queue --compact` (estados)
  e `unicornio-editor checklist POST_ID backups/<ID>/editorial.latest.json`.
- Plano ~40 posts/dia: 00h=5, 08h=7, 12h=8, 18h=10, 21h=10 (America/Sao_Paulo).
- `publish-cron.sh` roda `publish-ready` com gate `PUBLISH_ENABLED=true` +
  `EDITOR_DRY_RUN=false` (apenas neste script; o .env continua dry-run para o
  pipeline editorial). Retry com backoff em falha transitória de API/Cloudflare
  (3 tentativas) + log em `work/publish-window.log`.

## Estados e fila

- `READY` = preflight 100% (checklist completo passou no apply). SÓ `ready`
  é apto à publicação. `editorial.latest.json` NÃO significa pronto.
- `BLOCKED` = precisa rework; o card vem PRIMEIRO no lote com `fix`. Backoff:
  1ª falha +30m, 2ª +2h, 3ª -> **AWAITING_HUMAN** (sai da fila; humana decide com
  `retry` ou `discard`).
- `UNCERTAIN` / `SKIPPED` / `AWAITING_HUMAN` = fora da fila: não gera card,
  não re-tenta, não publica.
- O monitor (`queue --monitor`) só acorda o agente com trabalho ELEGÍVEL:
  rework fora de cooldown + pending recentes não processados. Idle custa zero
  tokens. O monitor NÃO acorda a cada tick por um bucket de parede — só quando
  um cooldown (next_retry_at) realmente expira.

## Rework (verificar -> corrigir -> publicar)

- Corrija pelo `fix` do card usando o draft (`unicornio-editor draft POST_ID`):
  altere só o componente apontado e re-aplique. NUNCA re-aplicar sem correção
  (o apply recusa de novo e conta tentativa/cooldown).
- Sem como corrigir (ex.: nenhuma imagem real da obra disponível):
  `unicornio-editor uncertain POST_ID --reason "..."` para tirar o post da fila —
  NUNCA force um apply que vai falhar nem deixe o post em loop de rework eterno.

## Pitfalls de conteúdo

- H2s de listicle PRECISAM ser numerados (`1. Obra: descrição`).
- Keyword: toda palavra significativa precisa aparecer no título E no corpo.
- Imagens: mínimo 2/4/6 SEM waiver; featured = key art da obra citada; sem
  imagem repetida no mesmo post; sem imagem transparente.
- Comandos de revisão humana: `retry POST_ID` (zera tentativas/cooldown),
  `discard POST_ID [--reason]` (sai da fila), `uncertain POST_ID --reason`
  (decisão do agente). Nenhum deles força READY.

## Pitfalls de mídia verificados em produção (2026-09-20)

- **Reuso da Media Library NÃO dispensa a verificação de origem.** Entrada com
  `media_library_id` + URL S3 do nosso upload é REJEITADA no apply com
  "verificacao de origem: imagem baixada nao consta na pagina de origem".
  Use a URL ORIGINAL da web (a que está no `work/keyart_cache.json`) junto da
  página de origem real; ela sim é listada na página e passa.
- **`media-validate` NÃO verifica origem nem largura; o `apply` sim.** Validar
  mídia nova no `media-validate` (visão/relevância) e só depois aplicar — mas
  nunca concluir que passou sem aplicar.
- **Largura mínima do inline = 640px de origem** (sem upscale). Imagens de
  arquivo da en.wikipedia (`File:*` não-livre) costumam vir em ~260-270px e
  falham com "inline image source is 269px wide (minimum 640px)".
- **A featured do card diz `normalize`, mas o apply pode rejeitá-la**: o gate de
  visão aceita (ex.: foto genérica de banco de imagens = UNRELATED) e há um gate
  extra de "arte plana/card de texto" que só roda no apply. Se a featured
  existente for foto de banco/card de manchete, TROQUE-a por uma key art/imagem
  real da obra no `media_plan` (`is_featured: true`).
- **Bing está envenenado para muitas queries** (devolve SERP de assunto
  completamente alheio — Gmail, computação quântica, bonecas). Yandex devolve as
  imagens certas mas com `source_page_url` VAZIO (inutilizável para origem).
  Quando o buscador falhar, ache a página real via web_search e confirme com
  `curl -s -A "<UA de navegador>" <pagina> | grep -o '<url-da-imagem>'` antes de
  montar o plano (o apply exige que a URL esteja listada na página).
- **`needs_trailer: true` exige `trailer_url` não-vazio** — senão o apply aborta
  com `trailer_url must be a non-empty string`. Sem trailer oficial real, use
  `needs_trailer: false`.

## Pitfalls de visão, texto e fontes (2026-09-21)

- **Visão da featured é CACHEADa** por `(sha256(url)[:12] + seo.title
  normalizado[:48])` em `work/vision_cache.json`. Foto de pessoa/empresa costuma
  sair `UNRELATED` e a rejeição cacheada vale para sempre naquele par. Cure
  escolhendo uma imagem que o `seo.title` NOMEIE (arte do jogo/hardware do tema)
  ou reaproveitando um par já `MATCH` (título com os mesmos 48 primeiros
  caracteres normalizados). A REDAÇÃO do `seo.title` muda o veredito: a mesma
  key art deu `text_banner` com um título e `MATCH 0.90 key_art` com outro.
- **`media-validate` TAMBÉM grava a decisão (inclusive a rejeição) e o
  `alt_text` do item entra no prompt**: faça um probe fiel antes de gastar a
  chamada (`context`/`category="game_artwork"`/`alt` do item iguais aos do apply).
- **Featured pode ser foto de produto da obra** (veredito `PARTIAL_MATCH`
  passa); só arte plana/texto é rejeitada (`image_is_mostly_flat`).
- **Travessão em/en é recusado pelo `qualidade_texto` em TODO o conteúdo**
  composto — inclusive no `alt_text`/`credit_text` que você escreve: normalize
  `–`/`—` para `-` no JSON inteiro.
- **Keyword do SEO**: `_keyword_in_text` exige os tokens significativos NA ORDEM
  no título E no corpo. Se o título e o keyword divergirem em ordem, ajuste o
  `focus_keyword` ("The Batman Part II adiado" falhou; "The Batman Part II" passou).
- **Fonte que bloqueia o downloader** (ex.: kchcomunicacion.com -> HTTP 400)
  pode falhar em SILÊNCIO no inline; o checklist então dimensiona o mínimo aos
  frames distintos reais que sobraram e o post vira READY assim mesmo — prefira
  Media Library/S3 ou origem que responda.
- **Busca web instável** (Bing devolvendo resultado sem relação, Google count=0,
  Yandex sem `source_page_url`): sem candidato com página de origem não há imagem
  validável -> registre `uncertain POST_ID --reason` com a evidência em vez de
  forçar apply.

## Segurança

- Nunca envie `status` num payload de update. Re-fetch o post imediatamente
  antes de qualquer escrita; aborte se não for `pending`.
- Nunca logue credenciais, tokens, cookies ou headers de autorização completos.
- Nunca use posts de produção para validação local.
