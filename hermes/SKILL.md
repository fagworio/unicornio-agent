---
name: unicorniohater-editor
description: Process WordPress pending posts in write mode, gated by the pre-publish checklist.
version: 0.4.0
metadata:
  hermes:
    tags: [wordpress, editorial, pending, safety]
---

# UnicornioHater Editorial Agent

Processe SOMENTE posts com status `pending`. References (só sob demanda — NUNCA
todas): `references/economia-contexto.md` (orçamento de sessão + contrato dos
comandos), `references/politica-imagens.md` (ANTES do `media_plan`),
`references/editorial-texto.md` (ao reescrever), `references/operacao.md` (quando
algo falhar) e `references/hash-imagens-analise.md` (frames repetidos).

## Non-negotiable safety

- Produção roda em write mode (`EDITOR_DRY_RUN=false`): `apply` grava conteúdo e
  meta, SEMPRE mantendo `pending`. Publicação SÓ pelo cron (`publish-ready`;
  gates PUBLISH_ENABLED=true + manifest/checklist 100%).
- Nunca envie `status` num payload de update. Re-fetch antes de escrever; aborte
  se o post não for `pending`.
- Pule conteúdo irrelevante/incerto sem tocar no WordPress. Crie snapshot antes.
- Nunca logue credenciais, tokens, cookies ou headers completos.
- Nunca use posts de produção para validação local.

## Orçamento da sessão (parar TAMBÉM é o trabalho)

- Meta de produção = `EDITOR_TARGET_READY_PER_RUN` (5 posts READY, distribuída
  entre sessões curtas). TETO de posts tocados = `EDITOR_MAX_POSTS_TOUCHED_PER_RUN`
  (default 2) — hard cap: `skipped`, `uncertain` e `blocked` liberam a vaga de
  READY, mas NÃO o teto de tocados.
- `cards --compact` traz `session{remaining_posts, context_bytes_used}` e corta o
  lote ao que ainda cabe. `count: 0` ou `stop` preenchido => **ENCERRE a sessão**:
  nada de buscar mais cards, nada de "aproveitar para adiantar outro post".
- `apply` de post NOVO acima do teto devolve `status: session_budget_exhausted`
  (nada foi escrito, nenhuma tentativa gasta): encerre a sessão.
- NUNCA simplifique o checklist para caber no orçamento — encerre a sessão.

## Fluxo editorial (sucesso = mínimo, falha = só o que corrigir)

1. `cards --compact` — UMA chamada com o DELTA por post (rework primeiro):
   `images{required,valid,missing,irrelevant,non_webp}`, `featured` (ação),
   `fix`, `requires_content`, `session`. Escreva os editoriais direto dos cards;
   NÃO abra blocked.json/logs/source. Fila geral: `queue --compact`.
2. Rode até a meta de 5 READY ou até o teto de tocados / `cards count: 0`.
   Falhou 1 correção num post → marque e SIGA. Máx. UMA correção por post por
   run (falhou de novo → PARE; 3ª falha → AWAITING_HUMAN).
3. REWORK (`blocked:true`): `draft POST_ID --for-fix` devolve SÓ o componente do
   gate + o erro (o artigo fica no arquivo) → corrija → `apply POST_ID
   patch.json --merge-draft --compact` (patch parcial mesclado pelo código).
   `requires_content: false` é enforçado: `content POST_ID` responde
   `content_not_required` nesse caso (use `--force` só se for reescrever).
4. Editorial: `site_relevance`, `seo`, `media_plan`, trailer. Jogo → `game_name`
   exato. `cleaned_html` OPCIONAL; `content POST_ID` SÓ quando
   `requires_content: true` ou reescrita real.
5. IMAGENS: antes do `media_plan`, leia UMA vez `references/politica-imagens.md`
   (contagem 2/4/6, listicle, fonte/licença/crédito, dimensões, posição no texto,
   reuso, fallback). Busca: `media-search-web "TERMO" --post-id POST_ID --needed N`
   (N = `images.missing`); listicle: UMA chamada
   `media-search-listicle "OBRA 1" "OBRA 2" ... --limit 3`.
6. Escolha a imagem: `decision: auto` → use `select` direto. `choose` →
   escolha entre 2-3 `options`. `reuse` → o acervo local já cobre o déficit (não
   houve busca web). `none` → nada utilizável: `uncertain`. O bloco `reuse`
   (Media Library/índice local) vem ANTES da web: use-o primeiro. Google Images é
   só índice; a página original é a fonte. Não faça pré-verificação manual.
7. Mídia nova → `media-validate editorial.json --post-id POST_ID` (1 chamada;
   `{valid, rejected, capacity{required,valid,missing}, featured{status,reason}}`).
8. `apply POST_ID editorial.json --compact` = preflight COMPLETO (editorial →
   mídia → conteúdo → checklist INTEIRO → só então grava). PASS → `status:ready`
   (manifest SHA-256). FAIL → `needs_rework` + `failed` + state/attempts; rascunho
   em editorial.draft.json.
9. Normalização técnica (WebP/dimensões) e links internos SÃO DO CÓDIGO. Só
   procure imagem nova quando o card pedir (`fix.find_inline_images > 0`,
   `featured.action: replace|provide`). NÃO inclua links internos no JSON.
10. NÃO rode `checklist` manualmente (o apply valida). `--dry-run` só sob demanda.
11. `skip`/`uncertain`/`awaiting_human` nunca publicam nem marcam READY.
    Publicação: SÓ o cron. Nunca publique manualmente.

O agente nunca muda um post para status de publicação. Créditos de mídia sempre
visíveis e rastreáveis à licença.

## Estados (fonte de verdade: meta `_hermes_state` no WordPress)

```text
NEW | PROCESSING | BLOCKED | READY | SKIPPED | UNCERTAIN | AWAITING_HUMAN | PUBLISHED
```

- READY = preflight 100%. SÓ `ready` publica.
- BLOCKED = rework; card vem primeiro com `fix`. Backoff: 1ª +30m, 2ª +2h, 3ª →
  AWAITING_HUMAN (`retry`/`discard` humano).
- UNCERTAIN / SKIPPED / AWAITING_HUMAN = fora da fila (não gera card, não
  re-tenta, não publica).
- Monitor (`queue --monitor`) só acorda com trabalho ELEGÍVEL; o hash muda apenas
  quando `next_retry_at` expira. Rework eterno = erro seu → use `uncertain`.

## Rework (verificar → corrigir → publicar)

- Corrija pelo `fix` do card **e só o componente apontado** (`draft --for-fix`);
  altere o mínimo e re-aplique com `--merge-draft`. NUNCA re-aplicar sem correção.
- Sem como corrigir (ex.: sem imagem real da obra): `uncertain POST_ID --reason`
  — NUNCA force apply que vai falhar.

## Economia de contexto (cron runs — todo token custa dinheiro)

- REUSO PRIMEIRO (mídia): `media-search-web` já devolve `reuse` (Media Library/
  índice local verificado). Só o déficit restante vai à web.
- UMA busca por obra, com `--needed` igual ao que FALTA. Nunca junte obras numa
  query ampla (mistura assuntos); no listicle use o comando em lote.
- `apply --compact` SEMPRE. `media-search-web`/`media-validate`/`cards` já são
  compactos por padrão; o JSON grande fica em `work/**` (auditoria).
- `list-pending --compact` e `prepare --compact` SEMPRE (~1.3 KB vs ~120 KB).
- Decida skip/uncertain SÓ pelo card. Escreva o editorial num arquivo e passe o
  caminho; nunca cole o JSON duas vezes.
- OMITA `cleaned_html` sem reescrita; com mídia nova ele é OBRIGATÓRIO. OMITA
  `seo` quando `seo_exists` e não houver mídia. NUNCA inclua CTA/Fonte no HTML.
- Tópicos: `matched_topics` precisa intersectar SITE_TOPICS. Alt de imagem sempre
  nomeia a obra (nunca genérico).
- Cada imagem do corpo DISTINTA (nunca o mesmo frame em crops/tamanhos
  diferentes); a featured não reaparece no corpo. Bloqueado por
  `imagens_similares`: remova as cópias e siga — o mínimo é dimensionado pelos
  frames distintos reais, NUNCA repita para atingir a cota.
- IMAGENS SÃO OBRIGATÓRIAS (2/4/6 sem waiver). Sem imagem real após busca
  honesta: `uncertain`. Jogo sem trailer oficial: nunca use fan-made.
- NUNCA leia `src/**`, `pyproject.toml`, `.env` nem testes — o CLI é a interface.
  Não repita comandos; não re-prepare post já visto.

## Diagnóstico barato (interativo)

- `scripts/diagnostico.sh` (fila + telemetria; read-only; custo < $0.01).
- `unicornio-editor telemetry` resume blocagens/resultados e o contexto por
  comando e por post (`context_bytes_by_command`, `by_post`,
  `context_bytes_per_ready`) distinguindo "não há imagem" de "busca falhou".
- `unicornio-editor telemetry --sessions` cruza com o `state.db` do Hermes:
  `tokens_per_ready`, `requests_per_ready`, `tool_context_bytes_per_ready`
  (métrica PRINCIPAL: o que voltou ao modelo), `cost_per_ready_usd` e, em mídia,
  `local_reuse_rate`, `web_searches_per_ready`, `vision_calls_per_ready`,
  `candidates_examined_per_ready` — a medida para comparar antes/depois.
- Cron/roteiro completo: `hermes/cron-install.sh` (idempotente).

## Operational pitfalls (detalhes em references/operacao.md)

- CLI lê env direto: `set -a && . ./.env && set +a && .venv/bin/...`.
- JANELA DE PUBLICAÇÃO SILENCIOSA = candidatos bloqueados pelo checklist (não é
  o cron quebrado).
- H2s de listicle numerados (`1. Obra: descrição`); keyword no título E no corpo.
- Revisão humana: `retry`, `discard`, `uncertain` — nenhum força READY.
- Visão da featured é CACHEADA por (url + `seo.title`): a redação do título muda
  o veredito; `media-validate` também grava a decisão no cache.
