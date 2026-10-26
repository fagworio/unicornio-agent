# UnicornioHater Editorial Agent

Automacao editorial para WordPress acionada pelo cron do Hermes Agent.

## Objetivo

Processar apenas posts `pending`, corrigir estrutura HTML, melhorar texto, preparar SEO, remover imagens inline importadas, descobrir imagens via providers configurados, baixar e publicar cópia local em WebP com direitos e crédito explícitos, adicionar imagem destacada/trailer quando fizer sentido, reconstruir CTA/fonte e manter o post em `pending`.

O sistema usa duas camadas:

1. **Hermes/LLM:** julgamento editorial, melhoria de texto, SEO, decisao sobre imagens e trailer.
2. **Codigo deterministico:** WordPress REST, backups, sanitizacao, CTA, fonte, WebP, limites, validacao e bloqueio de publicacao.

## Ambiente de desenvolvimento e testes

Valide alterações WordPress contra uma instalação real e isolada. O **Devilbox local** continua suportado; a homologação **WordPress Writer V2 E2E** do GitHub Actions é uma alternativa oficial e reproduzível, com WordPress, banco de dados e credenciais efêmeros. A suíte unitária ou mocks não substituem nenhum desses testes REST reais.

### Devilbox

O ambiente Docker está localizado em:

```text
/home/joaofagner/workfolder/devilbox/
```

Utilize esse ambiente para subir e executar os serviços necessários durante os testes.

### WordPress local

A instalação local do WordPress está em:

```text
/home/joaofagner/workfolder/devilbox/data/www/wordpress/htdocs
```

Para a homologação automatizada, consulte [docs/wordpress-e2e-homologation.md](docs/wordpress-e2e-homologation.md). O workflow separado provisiona os MU-plugins usados pelo projeto, executa o WordPressClient e WordPressWriterV2 reais e destrói o ambiente após a execução. Não requer nem utiliza dados, URLs ou credenciais de produção.

### Cenários de teste no Devilbox

Para cada modificação realizada:

1. Identifique quais tipos de posts e quais estados de dados são afetados pela mudança.
2. Crie ou ajuste **posts locais de teste** dentro dessa instalação do WordPress com o status *pending* para reproduzir os cenários necessários.
3. Utilize esses posts locais para testar o comportamento antes e depois da alteração.
4. Inclua cenários positivos, negativos e casos de borda relevantes.
5. Quando a lógica depender de campos, taxonomias, metadados, conteúdo, status ou outras características do post, configure os posts locais com combinações representativas desses dados.
6. Não dependa de posts do ambiente de produção para validar a implementação.
7. Não considere a tarefa concluída apenas porque o código foi alterado: execute os testes no WordPress local ou obtenha homologação E2E aprovada no GitHub Actions para o SHA exato, e confirme que o resultado obtido corresponde ao comportamento esperado.
8. Caso encontre regressões ou resultados inesperados, corrija a implementação e repita os testes.

Ao finalizar, informe quais cenários foram testados e o resultado de cada um.

## Filtro de relevancia do site

Antes de revisar qualquer texto, o Hermes deve decidir se o `content` realmente pertence a linha editorial do UnicornioHater. Games, videogames, plataformas, industria de jogos, tecnologia relacionada e entretenimento/cultura geek pertinente podem ser processados. Conteudo claramente estranho ao portal, spam, texto importado para o post errado ou assunto sem relacao deve ser **ignorado sem qualquer alteracao no WordPress**. Em caso de incerteza, o comportamento padrao tambem e `skip`.

O JSON editorial inclui:

```json
{
  "site_relevance": {
    "decision": "process",
    "confidence": 0.96,
    "reason": "Noticia sobre lancamento de videogame",
    "matched_topics": ["games", "lancamentos"]
  }
}
```

Se `decision=skip` ou a confianca ficar abaixo do limite configurado, o comando `apply` termina com `wordpress_changed=false` e o cron passa ao proximo `pending`.

## Garantias de seguranca

- O cliente WordPress sempre sobrescreve `status` para `pending` ao atualizar.
- `publish` nao faz parte do pipeline.
- Backup JSON antes do processamento.
- Fail-closed: se a validacao falhar, nao atualiza.
- Fail-closed de relevancia: conteudo fora da linha editorial ou incerto nao e editado.
- Limite de lote pequeno por execucao.
- Imagens importadas existentes: preservadas so com credito completo; descartadas sem ele.
- Estados operacionais persistidos no WordPress (`_hermes_state`): somente READY publica.
- CTA e Fonte sao gerados de forma canonica.
- `original_link` e a unica fonte da URL de referencia.

## Pipeline

O fluxo operacional atual é o `v2-cycle`, que mantém um lock durante a sessão
e repete rodadas preview→write até READY ou um limite seguro:

```text
Hermes cron
  -> unicornio_editor.cli v2-cycle
  -> checkpoint/journal do ciclo
  -> a cada rodada: scheduler NEW+RETRY
       -> preview autorizado
       -> v2-run write com os mesmos IDs e ordem
       -> pipeline EDITORIAL -> MEDIA -> COMPOSE -> VALIDATE
       -> readback/checkpoint/telemetria
  -> retoma rodada interrompida ou encerra por READY/orçamento/estagnação
  -> após estagnação, DRAINING fecha NEW e processa somente retries
  -> publisher separado: somente posts READY
```

O diagrama abaixo descreve o pipeline histórico de etapas internas; os
comandos legados não devem ser usados pelo cron.

```text
Hermes cron (monitor: so acorda quando a assinatura da fila muda)
  -> cards (UMA chamada, max 2 posts): delta exato de imagens
     (required/valid/missing/irrelevant/non_webp), diagnostico da featured
     (exists/relevant/webp/dimensions/action) e plano fix para rework
  -> editorial JSON estrito (draft salvo pelo apply ANTES da execucao pesada)
  -> media-validate --post-id (1 chamada) quando ha midia nova:
     capacidade media-first do listicle + visão antecipada da featured
  -> apply = PREFLIGHT COMPLETO:
     resolve editorial -> executa midia (download, WebP, upload, credito)
     -> normaliza tecnicamente (featured 1280x720 WebP; inline nao-WebP
        relevante convertida — sem LLM) -> monta conteudo (trailer + CTA + Fonte)
     -> checklist INTEIRO (backup, pending, relevancia, Fonte, 2/4/6, featured,
        WebP, dimensoes, trailer/waiver auditado, CTA, qualidade de texto,
        estrutura, schema)
     -> falha: needs_rework + estado blocked (backoff 30m/2h; 3a falha
        AWAITING_HUMAN) — nada e gravado
     -> sucesso: grava conteudo + meta _hermes_state=ready + Ready Manifest
        (SHA-256) — SOMENTE ready significa apto a publicar
  -> estados no WordPress (_hermes_state/_hermes_attempts/_hermes_next_retry_at/
     _hermes_last_error/_hermes_ready_hash/_hermes_policy_version)
  -> publicacao (cron, janelas 00/08/12/18/21): cada janela publica ate 5
     posts READY disponiveis (`PUBLISH_LIMIT=5`; menos de 5 sao publicados
     imediatamente, sem esperar completar o lote); publish-ready consulta SOMENTE
     READY; hash do manifest intacto
     -> publica sem revalidar; mudou (STALE) -> revalida com o checklist; falhou
     -> blocked (volta para rework do agente)
  -> qualidade garantida por codigo, nao por diligencia do LLM: o apply nunca
     grava post que o publish bloquearia; o publish apenas confirma
```

## Vision gate (custo controlado, gpt-4o-mini)

O `media-validate` executa antecipadamente o gate de visão da featured. A decisão
é transferida pelo cache para a URL final do WordPress, e o checklist confirma o
mesmo resultado sem uma segunda chamada tardia. Isso pega o caso que
regex/URL/hash não veem, como um CDN servindo imagem errada sob slug certo. Para
manter o custo baixo:

- **Prompt restrito**: metadados (ALT/URL/fonte) sao contexto, os PIXELS sao a
  evidencia. O modelo nunca tenta nomear a obra (evita o knowledge cutoff para
  noticias de 2026). Retorna Structured Outputs `{status, confidence, visual_type}`.
- **detail: low por padrao** (~2833 tok/img). A **featured** pode escalar para
  `high` quando ambígua; imagens inline permanecem nos gates determinísticos.
- **Cache por hash(imagem)+entidade** (`work/vision_cache.json`) evita re-analisar
  a mesma key art em varios posts.
- **Limites mecanicos** de chamadas por post (`EDITOR_VISION_MAX_LOW`).
- Chave: `OPENAI_API_KEY` (fallback `EDITOR_VISION_API_KEY`). Fail-closed.
- Custo medido localmente: ~US$ 0.44 por 1000 imagens em `detail: low`.

Config em `.env`: `EDITOR_VISION_ENABLED`, `EDITOR_VISION_DETAIL`,
`EDITOR_VISION_MAX_LOW`.

## Instalacao

```bash
cd /opt/unicorniohater-editorial-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[media-search]'
cp .env.example .env
```

O modo `auto` tenta `duckduckgo,bing,yandex,google` nessa ordem. A biblioteca
`ddgs` é opcional e instalada pelo extra `media-search`; sem ela, o fallback
DuckDuckGo registra `provider_unavailable` e a busca segue para os provedores
seguintes. `EDITOR_MEDIA_SEARCH_REQUIRED_PROVIDERS` define a estratégia mínima
que precisa ser comprovada para autorizar `EXHAUSTED_VERIFIED` (por padrão,
`bing,yandex`). Fallbacks opcionais podem falhar sem invalidar essa prova, mas
suas falhas permanecem na telemetria. Google HTML não contorna `js_required`,
CAPTCHA ou políticas de acesso; tais respostas são registradas como falhas e
nunca como resultados vazios verificados. Google Browser/Playwright não é
suportado. O backend DDG é não oficial e pode mudar/bloquear; o campo `source`
retornado pela biblioteca é preservado para observar sua origem aparente. Não
se presume independência em relação ao Bing: meça ganho marginal por imagens
distintas aprovadas, não pela quantidade bruta de URLs.
O adapter remove apenas o cabeçalho hop-by-hop incompatível com HTTP/2 e mantém
verificação TLS; `EDITOR_DDGS_CA_BUNDLE` pode apontar para um PEM legível, caso
o bundle padrão do OpenSSL não esteja acessível. Respostas HTTP diferentes de
200, inclusive `202`, são falhas técnicas, nunca prova de busca vazia.
O health report diferencia o bootstrap `vqd` do endpoint `i.js` e guarda apenas
estágio/status; o status `202`, sem evidência do corpo, não é classificado como
CAPTCHA nem aciona cooldown automático.

Preencha `.env` com um usuario WordPress de automacao e Application Password.

Producao opera em write mode (o `apply` grava de verdade, sempre mantendo
status `pending`; a publicacao so acontece via `publish-ready` com gate
`PUBLISH_ENABLED`):

```env
EDITOR_DRY_RUN=false
```

Teste:

```bash
unicornio-editor list-pending
unicornio-editor prepare 12345
unicornio-editor prepare-batch 12345 12346 --batch-id batch-local-01
unicornio-editor editorial-generate-batch work/batches/batch-local-01/editorial.input.json --root .
unicornio-editor maintenance-report posts.json
# Medir gasto de contexto por run (telemetria):
unicornio-editor telemetry --root .
# Preflight somente leitura do canary: auth, dois pending e Media Library:
unicornio-editor canary-preflight 12345 12346 --root .
# Filtrar a telemetria de um microbatch específico:
unicornio-editor telemetry --batch-id batch-local-01 --root .
# Descoberta de imagens via Google Images com filtro do editor
# (imgsz=xga = 1024x768, imgar=w = proporção larga, udm=2):
unicornio-editor media-search-web "redfall xbox series" --size xga --ratio w --limit 5
# Para listas: uma chamada compacta, com uma busca exata por obra em paralelo.
unicornio-editor media-search-listicle "Blue Box temporada 2" "Psyren anime" --limit 3

# Microbatch editorial: o apply continua isolado por post
unicornio-editor apply-batch editorial-batch.json --compact
unicornio-editor vision-batch vision-batch.json
unicornio-editor media-resolve-batch media-batch.json --compact
```

`prepare-batch` grava um envelope completo por post em
`work/batches/<batch_id>/`, além de snapshots individuais e
`editorial.input.json`, pronto para uma única inferência. O arquivo editorial
de microbatch usa `{schema_version: 1, batch_id, results: [{post_id, status, editorial}]}`;
um item pode ser `needs_retry` sem invalidar o outro.
`editorial-generate-batch` faz diretamente uma única chamada estruturada ao
provider configurado por `EDITORIAL_*`, sem tools ou loop Hermes, e grava
`editorial.output.json`, que pode ser enviado ao `apply-batch`.
O `apply-batch` valida o envelope antes de começar e chama o `apply` normal para
cada item; uma falha, lock, rework ou estado READY nunca é compartilhado com os
outros posts. Um retry do mesmo `batch_id` reconhece itens já READY como
`noop`; itens `needs_retry` são reenviados individualmente, sem reaplicar os
irmãos que já concluíram.

Quando houver vários candidatos de destaque, `vision-batch` aceita
`{schema_version: 1, batch_id, items: [{candidate_id, post_id, image_url,
subject, require_key_art}]}`. Apenas decisões visuais definitivas entram no
cache. O batch faz uma chamada `low`; se houver ambiguidade, faz no máximo uma
segunda chamada `high` somente com o subconjunto ambíguo.

`media-resolve-batch` recebe até dois posts no formato
`{batch_id, posts: [{post_id, subject, needed, query}]}`. Ele executa reuso da
Media Library, descoberta em paralelo, verificação da página de origem,
relevância e pHash sem nova inferência do modelo. Gera um plano compacto e,
quando necessário, `vision.input.json` para a única etapa multimodal seguinte.

A manutenção é somente relatório e nunca atualiza o WordPress. Para testes locais do Devilbox, consulte `tests/wordpress/README.md`.

A telemetria diferencia `hermes_model_requests`, `editorial_model_requests`,
`vision_provider_requests`, `tool_calls`, `external_http_requests` e
`model_cost_usd`; portanto um comando batch não é confundido com uma chamada
ao provider.

## Hermes

Copie ou linke `hermes/SKILL.md` para a pasta de skills do Hermes usando o nome `unicorniohater-editor`.

Depois instale o cron a partir da raiz do projeto:

```bash
./hermes/cron-install.sh
```

O Hermes suporta cron jobs com `--skill` e `--workdir`, e cada execucao ocorre em sessao isolada; por isso todas as regras criticas estao no projeto/skill e nao dependem da memoria de uma execucao anterior.

## Politica editorial

### Texto
- Melhorar abertura, clareza, fluidez, transicoes e escaneabilidade.
- Remover repeticao e linguagem artificial.
- Explicar relevancia sem clickbait.
- Preservar fatos, datas, nomes, numeros, plataformas e incertezas.
- Nunca inventar informacao.

### SEO
- Keyword principal natural.
- SEO title de ate 65 caracteres.
- Meta description entre 120 e 160 caracteres.
- H2 orientado ao leitor e ao assunto, nao a repeticao mecanica de keyword.
- Alt text descritivo nas novas imagens.

### Midia
- Imagens inline importadas: PRESERVADAS quando o figure carrega credito
  completo (validacao deterministica por codigo); sem credito completo sao
  removidas e exigem redescobrimento no media_plan.
- Escolher novas imagens por contexto da secao.
- Minimo OBRIGATORIO 2/4/6 (2 <= 600 palavras, 4 <= 1000, 6 acima; listicle =
  max(2, itens)) — sem waiver; o apply recusa abaixo do minimo.
- Listicle é planejado pela mídia: uma imagem inline distinta e validada por
  item; a featured não entra na conta. Quatro imagens inline permitem no máximo
  quatro itens. `media-validate editorial.json --post-id ID` informa capacidade
  e déficit antes do upload.
- Inserir apos um paragrafo que introduza visualmente o assunto, normalmente apos 2-4 paragrafos.
- Minimo de 3 paragrafos entre imagens.
- Converter para WebP antes do upload (normalizacao tecnica e automatica no apply).
- Google Images é somente índice de descoberta; a fonte é a página ORIGINAL da imagem (preview do Google não é fonte).
- Política de imagens: crédito visível é obrigatório, mas não concede permissão. O pipeline aceita CC0, domínio público, CC BY e CC BY-SA sem variantes NC/ND, com referência oficial correspondente; não aceita alegações livres de permissão. Antes do upload, a página original precisa declarar um Schema.org `ImageObject` com `contentUrl` igual ao URL direto da imagem e a mesma licença oficial. Licença genérica da página, metadado do agente ou evidência de outro asset não basta; fontes sem essa prova estruturada falham fechadas.
- Registrar separadamente URL da página original, autoria/crédito, licença, URL explícita dos termos e data da captura. A URL da origem não substitui a referência da licença.
- Baixar e hospedar a cópia na Media Library do WordPress, sem bucket/CDN externo.
- Inserir crédito visível com autor, origem e licença; o crédito é obrigatório em toda imagem.
- Sem crédito visível, descartar a imagem e procurar outra.

### Trailer

- Jogos usam somente trailer oficial validado; conteúdo fan-made nunca satisfaz
  o gate.
- Quando a busca oficial termina sem resultado, o código persiste query,
  provedor, horário e `official_not_found`, concedendo waiver auditável.
- Falha técnica da busca permanece bloqueante e não é tratada como ausência.

### Telemetria de produção

`unicornio-editor telemetry --root .` inclui posts únicos iniciados/bloqueados/
READY, taxa de READY na primeira tentativa, tentativas e duração médias, além do
funil de mídia por etapa e domínio: descoberta, preflight, visão da featured,
download, confirmação na origem, conversão e upload. Falhas do checklist também
são registradas individualmente, sem depender apenas da combinação de motivos.

### Links internos (código determinístico, sem IA)
- O apply adiciona links internos para categorias do portal na PRIMEIRA
  ocorrência de cada termo inequívoco do mapa (Netflix, PlayStation 5,
  Xbox Series X, PC, Marvel, DC Comics, Star Wars, Disney+, HBO Max, etc.),
  no máximo uma vez por URL, com link interno padrão (follow, sem
  target=_blank, sem rel=nofollow).
- Termos que dependem de contexto ("manga", "max" isolado, "teaser",
  "análise"/"crítica"/"review", "DC" isolado, Android/iOS isolados) NÃO
  recebem link automático — uma string replace não julga contexto.
- Nunca linka dentro de um `<a>` existente, nem dentro de headings
  (h1-h6), nem no meio de uma palavra (boundary de palavra completa).
- O enriquecimento roda no `compose_final_content` (mesmo conteúdo que o
  checklist valida e o manifest hasheia) e pode ser desligado com
  `EDITOR_INTERNAL_LINKS_ENABLED=false`. O mapa vive em
  `src/unicornio_editor/internal_links.py`.

### Fonte
Formato final:

```html
<hr />

<h3>Confira mais novidades em nosso Portal de <a href="https://prod.unicorniohater.com.br/noticias/">Notícias!</a></h3>

<hr />

<em>Fonte: <a href="URL_ORIGINAL" target="_blank" rel="nofollow noopener">Nome do Site</a>.</em>
```

## Fases de implantacao

### Fase 1 - Dry-run
Audita e produz relatorio/backup sem alterar WordPress.

### Fase 2 - Pending autonomo
Atualiza o post, mas sempre continua `pending`.

### Fase 3 - Descoberta e validação de mídia
Pesquisa candidatos pelos providers configurados, exige proveniência e declaração de licença estruturada vinculada ao `ImageObject` exato, valida identidade/relevância visual, baixa, converte para WebP, envia à Media Library local, registra crédito/licença e insere imagens e featured media. A prova de licença é validada na página de origem antes do upload; uma declaração sem vínculo ao asset é rejeitada.

### Fase 4 - Manutencao
Jobs separados e inicialmente somente `report` para:
- imagens quebradas;
- links quebrados;
- posts antigos sem CTA/fonte padrao;
- posts antigos com SEO fraco;
- imagens legadas sem WebP;
- featured image inexistente/quebrada;
- posts sem imagem suficiente;
- midia orfa.

## Estado das capacidades

| Capacidade | Onde vive | Estado |
|---|---|---|
| **Rank Math** (title/description/focus keyword nos metas do plugin) | `seo/rank_math.py` | ✅ |
| **Descoberta de imagens** (Bing/Yandex/Google agregados, com circuit breaker e backoff) | `media/search.py` | ✅ |
| **Validação de origem** (a imagem consta na página? bytes conferem?) | `media/source_verify.py` | ✅ |
| **Resolver de origem** para candidatos sem página (Yandex) | `media/source_resolver.py` | ✅ |
| **Registry de fontes oficiais** (entidade → publishers) | `media/official_sources.py` | ✅ |
| **Subject por item/H2** + score determinístico de evidência (gates A/B/C) | `media/evidence.py` | ✅ |
| **Dedupe visual** (pHash antes da seleção/upload) | `media/visual_hash.py` | ✅ |
| **Índice de mídia** (fingerprint + proveniência; reuso antes de custo novo) | `media/library_index.py` | ✅ |
| **Conversão WebP + upload** com crédito/licença | `media/converter.py`, `media/wordpress_media.py` | ✅ |
| **Vision gate** (só em caso ambíguo, depois da origem validada) | `media/vision_gate.py` | ✅ |
| **Trailer auditável** (evidência determinística) | `media/` | ✅ |
| **Estados persistentes** (`_hermes_*`) + `AWAITING_HUMAN` no WordPress | `state.py`, `workflow.py` | ✅ |
| **Reconciliação de estado** (`unicornio-editor reconcile`, somente leitura) | `reconcile.py` | ✅ |
| **Links internos idempotentes** (1 URL por artigo, canônica) | `internal_links.py` | ✅ |

### Decisões de política (fonte da verdade)

- **Publicação**: cada janela publica `min(READY disponíveis, 5)`. Se houver
  menos de 5, publica todos imediatamente; não espera completar o limite.
  Os READY excedentes permanecem para a próxima janela. `PUBLISH_LIMIT=5` é
  contado por publicação efetiva (`wordpress_changed`), não por candidato
  examinado.
- **Mídia**: nenhuma imagem entra porque o agente disse que está correta. A cadeia
  verificável é `query → página de origem → URL → bytes → subject → seção → hash`,
  com proveniência como **hard gate** (sem origem não existe ACCEPT) e visão/LLM
  restrita a casos ambíguos **com origem validada**.
