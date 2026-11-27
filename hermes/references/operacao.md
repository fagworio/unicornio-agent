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
  pipeline editorial). Cada execução grava primeiro em
  `work/publish-windows/.<window_id>.run.json`; somente a finalização grava
  atomicamente `work/publish-windows/<window_id>.json` e atualiza
  `work/publish-window.log` como compatibilidade da última janela. Não existe
  retry automático enquanto o CLI não produzir uma autorização confiável
  `retry_safe=true` + `write_started=false`; exit code, mensagens ou stdout
  vazio nunca autorizam replay. Preserve `work/publish-windows/` em backups,
  snapshots e deploys.
- A confirmação inicial permite **uma nova leitura GET**, sem reenviar o POST,
  se o post já estiver publicado mas sem identificador de operação na resposta,
  ou se a leitura falhar transitoriamente. Um ID diferente não autoriza essa
  releitura. O evento `publish_readback` separa ausência, divergência e falha
  HTTP/transporte, sem gravar o corpo da resposta nem o metadado bruto. Se a
  confirmação não passar, o marcador durável continua protegendo a operação.
  Se uma leitura já confirmou `publish`, uma projeção posterior `pending` não
  libera o marcador nem autoriza uma segunda publicação.

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
- Sem candidato de mídia após busca honesta: deixe `media_plan` vazio e execute
  `apply`. Depois de `site_relevance=process`, a ausência de mídia é falha de
  checklist e entra em `PARTIAL`, nunca em `uncertain` ou `AWAITING_HUMAN` direto.

## Pitfalls de conteúdo

- H2s de listicle PRECISAM ser numerados (`1. Obra: descrição`).
- Keyword: toda palavra significativa precisa aparecer no título E no corpo.
- Imagens do corpo: mínimo 2/4/6 conforme checklist e sem repetição. No V1
  padrão a capa é o anexo original do WordPress, normalizado para WebP 1280×720;
  sem Vision de assunto ou busca de substituta.
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
- **Capa no card V1**: `ok` mantém o anexo; `normalize` converte a imagem original;
  `manual` significa ausência que precisa ser corrigida no WordPress, nunca
  pela busca editorial. Não entregue `is_featured: true` no plano do V1 padrão.
- **Bing está envenenado para muitas queries** (devolve SERP de assunto
  completamente alheio — Gmail, computação quântica, bonecas). Yandex devolve as
  imagens certas mas com `source_page_url` VAZIO (inutilizável para origem).
  Quando o buscador falhar, ache a página real via web_search e confirme com
  `curl -s -A "<UA de navegador>" <pagina> | grep -o '<url-da-imagem>'` antes de
  montar o plano (o apply exige que a URL esteja listada na página).
- **`needs_trailer: true` exige `trailer_url` não-vazio** — senão o apply aborta
  com `trailer_url must be a non-empty string`. Sem trailer oficial real, use
  `needs_trailer: false`. O campo indica uma proposta, não um gate de READY.
  Se a verificação/busca falhar, o post segue sem trailer e sem dispensa inventada.
  O embed não gera thumbnail nem substitui a imagem destacada do artigo.

## Pitfalls de visão, texto e fontes (2026-09-21)

- **A decisão visual das imagens internas** é ligada aos bytes e ao contexto.
  Não use aprovação do cache de outra imagem como evidência. A capa original
  é avaliada tecnicamente, não pelo assunto.
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
