---
name: unicorniohater-editor
description: Process WordPress pending posts in write mode, gated by the pre-publish checklist.
version: 0.2.0
metadata:
  hermes:
    tags: [wordpress, editorial, pending, safety]
---

# UnicornioHater Editorial Agent

Process only WordPress posts with status `pending`. Read the linked references
when needed: `references/politica-imagens.md` (regras completas de imagem —
consulte ANTES de montar o media_plan) e `references/operacao.md` (pitfalls de
produção — consulte quando algo falhar ou parecer estranho).

## Non-negotiable safety

- Produção roda em write mode (`EDITOR_DRY_RUN=false` no .env): `apply` grava
  conteúdo e meta, SEMPRE mantendo status `pending`. Publicação SÓ pelo cron
  (`publish-ready`, gates PUBLISH_ENABLED=true + manifest/checklist 100%).
- Nunca envie `status` num payload de update.
- Re-fetch o post imediatamente antes de qualquer escrita; aborte se não for
  `pending`.
- Pule conteúdo irrelevante/incerto sem tocar no WordPress. Crie snapshot JSON
  antes de processar.
- Nunca logue credenciais, tokens, cookies ou headers de autorização completos.
- Nunca use posts de produção para validação local.

## Fluxo editorial (economia de tokens — sucesso = mínimo, falha = só o que corrigir)

1. `unicornio-editor cards` — UMA chamada devolve os cards do lote (NO MÁXIMO
   2; rework primeiro, depois novos). Cada card traz o DELTA exato:
   `images: {required, valid, missing, irrelevant, non_webp}`, diagnóstico da
   `featured` (`exists / relevant / webp / dimensions / action`) e, para
   bloqueados, o plano `fix`. Escreva os editoriais direto dos cards — nunca
   abra blocked.json/checklist/logs/source para descobrir o que corrigir.
   Para o estado geral da fila: `unicornio-editor queue` (read-only; o monitor
   do cron só desperta com trabalho elegível — idle custa zero tokens).
2. Processe NO MÁXIMO 2 posts por run e pare em ~30 tool calls. Máximo UMA
   tentativa de correção por post por run: apply falhou → corrija → apply de
   novo → se falhar de novo, PARE e deixe para a próxima run (backoff/cooldown
   cuidam do ritmo; 3ª falha vira AWAITING_HUMAN).
3. REWORK (card com `blocked: true`): leia `blocked_reason` + `fix` do card —
   o código já disse o que corrigir (quantas imagens faltam, featured
   normalizar/substituir, lista, texto). Carregue o rascunho com
   `unicornio-editor draft POST_ID` e corrija SOMENTE o componente apontado
   pelo fix — nunca reescreva texto/SEO que já estão bons. Ex.: faltam 2
   imagens → só adicione itens ao `media_plan` do draft e re-aplique.
4. Produza o editorial JSON estrito com `site_relevance`, `seo`, `media_plan`
   e trailer. Jogo → `game_name` exato (o código acha/valida o trailer —
   nunca invente URL). `cleaned_html` é OPCIONAL: omita quando o texto
   preparado já está bom (no-rewrite; o apply reusa o conteúdo limpo
   determinístico); inclua SÓ quando realmente reescrever — e aí use
   `unicornio-editor content POST_ID` em vez de abrir o prepared.json inteiro.
5. IMAGENS: consulte `references/politica-imagens.md` antes do media_plan.
   RESUMO CRÍTICO: (a) mínimo 2/4/6 SEMPRE (2 <= 600 palavras, 4 <= 1000, 6
   acima; listicles = max(2, nº itens)) — post sem o mínimo NÃO vira READY;
   (b) toda imagem retrata EXATAMENTE a obra citada (nunca conceito genérico);
   (c) featured = key art da obra citada, nunca arte da matéria; (d) inline
   640-1280px; (e) URL direta listada na página de origem (verificação
   byte-a-byte no apply); (f) crédito visível em toda imagem;
   (g) SEM imagem transparente: o pipeline acha o canal alpha e achata sobre
   fundo branco antes do upload/insert (o WebP publicado nunca é
   transparente); imagem (quase) totalmente transparente é REJEITADA — troque
   por uma versão com fundo.
   (h) SEM imagem REPETIDA no mesmo post: cada imagem do corpo deve ser
   distinta (mesma obra = imagens/ângulos/capturas diferentes, nunca a mesma
   URL usada várias vezes). O checklist `imagens_duplicadas` bloqueia — não
   reuse a mesma imagem em múltiplos `media_plan` nem como featured + inline.
   ORDEM DE BUSCA (regra 2026-08-21): comece na WEB — Google Images/web_search
   para key art em sites de notícias, páginas oficiais, lojas (Steam CDN
   header.jpg etc.) — extraia a URL DIRETA da página original e use
   `license: "Uso com crédito"` com `license_url` = a página original.
   Wikimedia Commons é FALLBACK (pouca key art oficial e rate-limit 429) —
   não trave re-tentando Wikimedia; troque para fontes web imediatamente.
6. Antes do apply com mídia nova, valide o plano com `unicornio-editor
   media-validate editorial.json` (1 chamada; {valid, rejected}) — corrija o
   que rejeitar.
7. `unicornio-editor apply POST_ID editorial.json --compact` — o apply É o
   preflight COMPLETO: resolve editorial → executa mídia → monta conteúdo →
   roda o checklist INTEIRO → só então grava. PASS → `status: ready` (o post
   fica READY com manifest; o publish-ready apenas confirma o hash). FAIL →
   `status: needs_rework` com `failed` (o que falhou + quanto falta) +
   `state/attempts/next_retry_at`; o editorial fica arquivado em
   `editorial.blocked.json` e o rascunho preservado em `editorial.draft.json`.
   O relatório completo vai para `backups/<ID>/apply.latest.json`.
8. Normalização técnica É DO CÓDIGO, não sua: featured relevante fora do
   padrão (JPEG/PNG/dimensão errada) é re-baixada e convertida para 1280x720
   WebP automaticamente no apply; imagens inline relevantes não-WebP idem
   (sem nova busca semântica). SÓ procure imagem nova quando o card disser
   `featured.action: replace|provide` ou `fix.find_inline_images > 0`.
8b. LINKS INTERNOS SÃO DO CÓDIGO, não seus: o apply adiciona (determinístico,
    sem IA) um link interno de categoria na PRIMEIRA ocorrência de cada termo
    inequívoco do mapa (Netflix, PlayStation 5, Xbox Series X, PC, Marvel,
    DC Comics, Star Wars, etc.), no máximo uma vez por URL, com link padrão
    follow e sem target=_blank. Termos que dependem de contexto ("manga",
    "max" isolado, "teaser", "análise", "crítica", "DC" isolado, Android/iOS
    isolados) NÃO recebem link automático. Você NÃO precisa nem deve incluir
    esses links no JSON editorial: o apply os insere sozinho. Se o conteúdo
    já tiver o termo dentro de um <a> ou heading, o código respeita.
9. NÃO rode `checklist` manualmente: o apply já valida antes de gravar e o
   publish-ready re-valida por manifest (hash). `--dry-run` NÃO é obrigatório:
   use-o apenas em rework complexo, mídia nova, JSON que já falhou ou quando
   investigar um gate.
10. Inspecione o resumo e o backup. `skip`/`uncertain`/dry-run devem ter
    `wordpress_changed=false`.
11. Publicação: só o cron (`hermes/publish-cron.sh` -> `publish-ready`), que
    publica SOMENTE posts READY com manifest íntegro; se algo mudou desde o
    apply (STALE) ele revalida com o checklist completo. Nunca publique
    manualmente fora desse fluxo.

O agente nunca muda um post para status de publicação. Todos os créditos de mídia
devem ficar visíveis e rastreáveis à evidência da licença.

## Estados operacionais (fonte de verdade: meta `_hermes_state` no WordPress)

```text
NEW | PROCESSING | BLOCKED | READY | SKIPPED | UNCERTAIN | AWAITING_HUMAN | PUBLISHED
```

- **READY** = preflight 100% (checklist completo passou no apply). SÓ `ready`
  significa apto à publicação. `editorial.latest.json` NÃO significa pronto.
- **BLOCKED** = precisa rework; o card vem PRIMEIRO no lote com `fix`.
  Backoff: 1ª falha +30m, 2ª +2h, 3ª → **AWAITING_HUMAN** (sai da fila;
  humana decide com `retry` ou `discard`).
- **UNCERTAIN / SKIPPED / AWAITING_HUMAN** = fora da fila: não gera card, não
  re-tenta, não publica.
- O monitor (`queue --monitor`) só acorda o agente com trabalho ELEGÍVEL:
  rework fora de cooldown + pending recentes não processados. O rework NÃO
  reativa por bucket de parede: o hash muda só quando um `next_retry_at`
  (cooldown real) expira — bloqueio em cooldown não acorda o agente a cada
  tick. Rework eterno = erro seu: use `uncertain` para post não-corrigível.

## Loop verificar -> corrigir -> publicar (blocked/rework)

- O card sinaliza rework (`blocked: true` + `blocked_reason` + `fix`). Posts
  reabertos vêm PRIMEIRO no lote — corrija-os ANTES de posts novos.
- Corrija pelo `fix` do card usando o draft (`unicornio-editor draft POST_ID`):
  altere só o componente apontado e re-aplique. NUNCA re-aplicar sem correção
  (o apply recusa de novo e conta tentativa/cooldown).
- Sem como corrigir (ex.: nenhuma imagem real da obra disponível):
  `unicornio-editor uncertain POST_ID --reason "..."` para tirar o post da
  fila — NUNCA force um apply que vai falhar.
- `apply` com status `needs_rework` NÃO é final: corrija e tente de novo no
  mesmo lote (máx. 1 correção por post por run); o post continua pending e
  fora da publicação.

## Token economy (cron runs — todo token custa dinheiro)

- KEY ART CACHE FIRST: antes de QUALQUER busca web, consulte
  `work/keyart_cache.json` (obra -> imagens verificadas) e
  `unicornio-editor media-search TERMO --limit N` (reuso da biblioteca). Busque na
  web só se nenhum dos dois servir. Imagem verificada nova → REGISTRE no cache.
- UMA busca por obra; verifique por fragmento (grep) da página original — nunca
  despeje HTML no terminal (salve em /tmp).
- `apply --compact` SEMPRE (resumo no terminal, relatório completo em
  `backups/<id>/apply.latest.json`); `--dry-run` apenas sob demanda (rework,
  transformação grande, mídia complexa, JSON que já falhou, investigação).
- `media-validate` antes do apply quando o media_plan tiver mídia nova
  (1 chamada; evita apply com itens rejeitados).
- `prepare`/`content` SO quando for reescrever o texto — no-rewrite não precisa.
- Decida skip/uncertain SÓ pelo card; não prepare nem busque imagem de conteúdo
  irrelevante.
- Batch: EDITOR_BATCH_LIMIT=2 — processe NO MÁXIMO 2 posts por execução e
  pare em ~30 tool calls (run curta = contexto pequeno = barata).
- `list-pending --compact` e `prepare --compact` SEMPRE (~1.3 KB vs ~120 KB).
- NUNCA leia src/**, pyproject.toml, .env nem testes para "entender o fluxo" —
  o CLI é a interface; exceção: erro não autoexplicativo → leia SÓ a função do
  traceback.
- Não repita comandos; não re-prepare post já visto.
- Escreva o editorial num arquivo (write_file) e passe o path; nunca cole o JSON
  duas vezes no chat.
- OMITA `cleaned_html` sem reescrita real; OMITA `seo` quando o card mostra
  `seo_exists` (o código herda). NUNCA inclua CTA/Fonte no cleaned_html (o
  builder insere o canônico e remove duplicados).
- Skip conservador: só com confidence >= 0.9; abaixo disso o apply grava
  `uncertain.json` (post fica pending, fora da fila) — não force skip final.
- Tópicos: `matched_topics` precisa intersectar SITE_TOPICS.
- Alt de imagem SEMPRE nomeia a obra ("Redfall key art"), nunca genérico.
- IMAGENS SÃO OBRIGATÓRIAS (mínimo 2/4/6 SEM waiver): todo editorial
  processado DEVE ter media_plan com imagens reais verificadas até o mínimo do
  word count. O apply RECUSA gravar editorial abaixo do mínimo (status
  `needs_rework`, arquiva `editorial.blocked.json` e o post volta à fila de
  rework). NÃO existe "não buscar imagens quando não agregar valor" — buscar é
  parte do prepare, sempre. Se após busca honesta não houver imagem real
  relevante, registre `uncertain` (decisão do agente) em vez de aplicar sem
  imagens.

## Diagnóstico barato (sessões interativas — todo token custa dinheiro)

- Para "verificar X" (fila, publicações, custo, crons, tokens): rode UMA chamada
  `scripts/diagnostico.sh` (fila editorial; read-only). Custo < $0.01 por
  verificação.
- NUNCA explore `src/**`, `.env`, `backups/**`, logs ou JSONs grandes para
  diagnosticar — o script e o CLI (`queue`, `list-pending --compact`) são a
  interface.

## Operational pitfalls (resumo — detalhes em references/operacao.md)

- CLI lê env direto: sempre `set -a && . ./.env && set +a && .venv/bin/...`
  (sem o env ele cai na URL mock e "time out").
- `list-pending` consulta status=pending no servidor (filtro local retorna []).
- JANELA DE PUBLICAÇÃO SILENCIOSA = candidatos bloqueados pelo checklist (não é o
  cron quebrado). Diagnostique com `unicornio-editor queue` (estados) e
  `unicornio-editor checklist POST_ID backups/<ID>/editorial.latest.json`.
- H2s de listicle PRECISAM ser numerados (`1. Obra: descrição`).
- Keyword: toda palavra significativa precisa aparecer no título E no corpo.
- Comandos de revisão humana: `retry POST_ID` (zera tentativas/cooldown),
  `discard POST_ID [--reason]` (sai da fila), `uncertain POST_ID --reason`
  (decisão do agente). Nenhum deles força READY.
