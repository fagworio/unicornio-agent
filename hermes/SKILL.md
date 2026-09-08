---
name: unicorniohater-editor
description: Process WordPress pending posts in write mode, gated by the pre-publish checklist.
version: 0.3.0
metadata:
  hermes:
    tags: [wordpress, editorial, pending, safety]
---

# UnicornioHater Editorial Agent

Process only WordPress posts with status `pending`. Consulte as references quando
precisar: `references/politica-imagens.md` (regras de imagem — ANTES do
media_plan), `references/editorial-texto.md` (qualidade de texto/SEO e listas —
ao reescrever) e `references/operacao.md` (pitfalls — quando algo falhar).

## Non-negotiable safety

- Produção roda em write mode (`EDITOR_DRY_RUN=false` no .env): `apply` grava
  conteúdo e meta, SEMPRE mantendo `pending`. Publicação SÓ pelo cron
  (`publish-ready`; gates PUBLISH_ENABLED=true + manifest/checklist 100%).
- Nunca envie `status` num payload de update. Re-fetch antes de escrever;
  aborte se o post não for `pending`.
- Pule conteúdo irrelevante/incerto sem tocar no WordPress. Crie snapshot antes.
- Nunca logue credenciais, tokens, cookies ou headers completos.
- Nunca use posts de produção para validação local.

## Fluxo editorial (economia de tokens — sucesso = mínimo, falha = só o que corrigir)

1. `unicornio-editor cards --compact` — UMA chamada com o DELTA exato por post (até 5 =
   EDITOR_BATCH_LIMIT; rework primeiro): `images:{required,valid,missing,irrelevant,non_webp}`,
   diagnóstico da `featured` e plano `fix` para bloqueados. Escreva os editoriais
   direto dos cards; não abra blocked.json/logs/source. Fila geral: `queue --compact`.
2. A meta do run é **5 posts READY** (EDITOR_BATCH_LIMIT), não apenas 5 cards
   iniciais. `skipped`, `uncertain`, `blocked` em cooldown ou `awaiting_human`
   liberam a vaga: depois de registrá-los, rode `cards` outra vez e trilhe o
   próximo `pending` elegível. Só encerre antes de 5 READY quando `cards`
   retornar `count: 0`. Nunca retome o mesmo post que acabou de sair da fila.
   **NUNCA gaste o run num único post**: se um post falhou 1 correção, marque e
   SIGA para os próximos. Máx. UMA correção por post por run (falhou → corrija →
   re-aplique; falhou de novo → PARE; 3ª falha → AWAITING_HUMAN e siga para o
   próximo).
3. REWORK (`blocked:true`): use `blocked_reason` + `fix` do card, carregue o
   draft (`draft POST_ID`) e corrija SÓ o componente apontado — nunca reescreva
   texto/SEO bons.
4. Editorial estrito com `site_relevance`, `seo`, `media_plan`, trailer. Jogo →
   `game_name` exato (código acha/valida o trailer). `cleaned_html` OPCIONAL
   (omita no no-rewrite; use `content POST_ID` só para reescrever).
5. IMAGENS: antes de montar qualquer `media_plan`, leia UMA vez
   `references/politica-imagens.md`. Ela é a fonte completa para contagem
   2/4/6, listicles (1 item = 1 imagem), fonte/licença/crédito, dimensões,
   busca, reuso, fontes byte-estáveis e fallback. Regras curtas: obra exata,
   featured paisagem/key art, URL direta listada na página original, crédito
   visível, sem repetição/transparência. Busca normal: `media-search-web`;
   listicle: UMA chamada `media-search-listicle "OBRA 1" "OBRA 2" ...`.
   Não faça pré-verificação manual: `media-validate` e `apply` confirmam
   origem byte-a-byte. Google Images é só índice; a página original é a fonte.
6. Mídia nova → valide antes: `media-validate editorial.json --post-id POST_ID`
   (1 chamada; `{valid,rejected,listicle,featured_vision}`). O preflight usa o
   título real do WordPress, exclui a featured da capacidade do listicle e roda
   visão da featured somente quando a fonte for ambígua. Pares oficiais
   fortemente evidenciados (anime.com→TMDB, JustWatch→images.justwatch) passam
   pelo gate determinístico e não consomem visão.
7. `apply POST_ID editorial.json --compact` = preflight COMPLETO (resolver
   editorial → mídia → conteúdo → checklist INTEIRO → só então grava). PASS →
   `status:ready` (manifest SHA-256; publish-ready confirma o hash). FAIL →
   `needs_rework` + `failed` + state/attempts/next_retry_at; editorial arquivado
   em editorial.blocked.json; rascunho em editorial.draft.json.
8. Normalização técnica É DO CÓDIGO: featured fora do padrão é re-baixada para
   1280x720 WebP; inline não-WebP relevante idem. SÓ procure imagem nova quando o
   card disser `featured.action: replace|provide` ou `fix.find_inline_images > 0`.
8b. Links internos SÃO DO CÓDIGO: o apply insere (determinístico) link interno de
    categoria na 1ª ocorrência de cada termo inequívoco (Netflix, PlayStation 5,
    Marvel, etc.), 1x por URL, follow sem target=_blank. Termos de contexto
    (manga, max isolado, teaser, análise, DC isolado, Android/iOS) NÃO recebem
    link automático. NÃO inclua esses links no JSON; o apply insere sozinho.
9. NÃO rode `checklist` manualmente (apply já valida; publish-ready re-valida por
   manifest). `--dry-run` só sob demanda (rework complexo, mídia nova, JSON que
   falhou, investigar gate).
10. `skip`/`uncertain`/`awaiting_human` nunca publicam nem marcam READY. Em
    write mode, podem gravar somente a base determinística segura (CTA, Fonte,
    links internos e sanitização); o editorial que falhou fica no draft.
11. Publicação: só o cron (`publish-ready`), SÓ posts READY com manifest íntegro;
    STALE revalida com checklist. Nunca publique manualmente.

O agente nunca muda um post para status de publicação. Créditos de mídia sempre
visíveis e rastreáveis à licença.

## Estados (fonte de verdade: meta `_hermes_state` no WordPress)

```text
NEW | PROCESSING | BLOCKED | READY | SKIPPED | UNCERTAIN | AWAITING_HUMAN | PUBLISHED
```

- READY = preflight 100% (checklist passou no apply). SÓ `ready` publica.
- BLOCKED = rework; card vem primeiro com `fix`. Backoff: 1ª +30m, 2ª +2h,
  3ª → AWAITING_HUMAN (`retry`/`discard` humano).
- UNCERTAIN / SKIPPED / AWAITING_HUMAN = fora da fila (não gera card, não re-tenta,
  não publica).
- Monitor (`queue --monitor`) só acorda com trabalho ELEGÍVEL (rework fora de
  cooldown + pending não processados). NÃO reativa por bucket de parede: o hash
  muda só quando `next_retry_at` expira. Rework eterno = erro seu → use
  `uncertain`.

## Rework (verificar → corrigir → publicar)

- Corrija pelo `fix` do card usando o draft; altere só o componente apontado e
  re-aplique. NUNCA re-aplicar sem correção.
- Sem como corrigir (ex.: sem imagem real da obra): `uncertain POST_ID --reason`
  — NUNCA force apply que vai falhar.
- `needs_rework` não é final: corrija e tente de novo no mesmo lote (máx. 1).

## Token economy (cron runs — todo token custa dinheiro)

- KEY ART CACHE FIRST: `work/keyart_cache.json` + `media-search TERMO` (reuso da
  Media Library) antes de QUALQUER busca web. Imagem nova verificada → registre.
- UMA busca por obra. Em listicles, agrupe essas buscas numa única chamada
  `media-search-listicle "OBRA 1" "OBRA 2" ... --limit 3`: o comando executa
  uma consulta EXATA por obra em paralelo e devolve uma resposta compacta.
  Nunca junte todos os títulos numa única query ampla, pois ela mistura obras.
  NÃO baixe páginas de origem nem inspecione HTML
  manualmente — o apply verifica; `media-validate` valida em 1 chamada.
- `apply --compact` SEMPRE; `--dry-run` só sob demanda.
- **paragraph_index do media_plan**: imagens entram APÓS o parágrafo alvo, exigem >=3 parágrafos de distância entre si e o índice máximo é `len(</p> do conteúdo) - 2` (senão: `media must be inserted between paragraphs`). A featured NÃO entra no insert (vai como featured_media), então não ocupa vaga de spacing — mas o índice dela conta na validação do plano. Conte os blocos com `content POST_ID` antes de montar o plano.
- `media-validate editorial.json --post-id POST_ID` antes do apply com mídia
  nova (1 chamada); ajuste o número de itens ao `verified_inline_capacity`.
- `prepare`/`content` SÓ para reescrever (no-rewrite não precisa).
- Decida skip/uncertain SÓ pelo card.
- Batch: respeite ``EDITOR_MAX_POSTS_PER_RUN`` (padrão: 5) como teto de posts
  que chegam a READY. Uma saída definitiva sem READY não consome a vaga: busque
  outro card enquanto houver `pending` elegível. Mantenha uma sequência compacta
  de ferramentas por post para amortizar o custo da sessão.
- `list-pending --compact` e `prepare --compact` SEMPRE (~1.3 KB vs ~120 KB).
- NUNCA leia src/**, pyproject.toml, .env nem testes — o CLI é a interface.
- Não repita comandos; não re-prepare post já visto.
- Escreva o editorial num arquivo e passe o path; nunca cole o JSON 2x no chat.
- OMITA `cleaned_html` sem reescrita; OMITA `seo` quando `seo_exists`. NUNCA
  inclua CTA/Fonte no cleaned_html.
- Skip conservador: só com confidence >= 0.9; abaixo, o apply grava uncertain.
- Tópicos: `matched_topics` precisa intersectar SITE_TOPICS.
- Alt de imagem SEMPRE nomeia a obra, nunca genérico.
- Jogo sem trailer oficial: nunca use fan-made; o código registra a busca e só
  concede waiver quando o resultado auditado é `official_not_found`. Falha da
  busca não concede waiver.
- IMAGENS SÃO OBRIGATÓRIAS (2/4/6 SEM waiver): sem imagens reais até o mínimo,
  o apply recusa. Se após busca honesta não houver imagem, registre `uncertain`.

## Diagnóstico barato (interativo)

- `scripts/diagnostico.sh` (fila + telemetria; read-only; custo < $0.01).
- `unicornio-editor telemetry` resume blocagens/resultados (apply_ready,
  apply_blocked+motivo, apply_uncertain, apply_skipped, media_search_empty,
  cmd_output) de `work/telemetry.jsonl`. Distingue "não há imagem" de "busca
  falhou/bloqueada".
- NUNCA explore src/**/.env/backups/**/logs grandes — o CLI é a interface.

## Operational pitfalls (detalhes em references/operacao.md)

- CLI lê env direto: `set -a && . ./.env && set +a && .venv/bin/...`.
- `list-pending` consulta status=pending no servidor.
- JANELA DE PUBLICAÇÃO SILENCIOSA = candidatos bloqueados pelo checklist (não é
  o cron quebrado). Diagnostique com `queue --compact` + `checklist`.
- H2s de listicle numerados (`1. Obra: descrição`); keyword no título E no corpo.
- Revisão humana: `retry POST_ID`, `discard POST_ID [--reason]`, `uncertain
  POST_ID --reason`. Nenhum força READY.
