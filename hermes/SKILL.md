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

## Executor oficial V2 — regra de precedência

O executor editorial operacional é:

```bash
set -a; . ./.env; set +a
PYTHONPATH=.:src .venv/bin/python -m unicornio_editor.cli v2-cycle --root .
```

O `v2-cycle` executa um preview e um write-mode pinado por rodada, persiste o
checkpoint em `work/v2-cycle/state.json` e retoma automaticamente uma rodada
interrompida. A seleção de cada preview é a única autorização de escopo: não
adicionar `--post-id`, não ampliar allowlists, não alterar cooldowns e não
processar IDs fora de `selected_ids`. O `v2-run` continua disponível como
executor de uma única rodada e coordena EDITORIAL → MEDIA → COMPOSE → VALIDATE
→ READY/RETRY/HUMAN_REQUIRED.

O ciclo encerra quando alcança `EDITOR_V2_CYCLE_READY_TARGET`, esgota orçamento,
fica sem trabalho elegível, conclui a fase `DRAINING` de retries após
estagnação ou encontra divergência crítica. Em `DRAINING`, novas admissões são
fechadas e somente retries elegíveis podem entrar.
Falhas de autorização ou executor deixam o checkpoint em estado retomável; não
use `--fresh` para descartar uma execução interrompida.
Retries editoriais fora de `seen_post_ids` são novas admissões para o ciclo e
nunca podem entrar em `DRAINING`. Uma autorização pendente incompatível pode ser
invalidada somente sob o lock, quando não há journal nem evidência de write no
readback; o evento `round_authorization_invalidated` preserva a seleção e os
contadores históricos sem contar uma rodada não executada. Qualquer evidência de
write exige reconciliação por post. O limite de duração considera apenas tempo
de execução, não o intervalo de pausa entre invocações do cron.

Os comandos do lifecycle legado (`prepare-batch`, `editorial-generate-batch`,
`media-resolve-batch`, `apply-batch`, `media-search-web` e `apply`) não podem ser
executados pelo cron, pelo operador ou por uma reinstalação. Qualquer referência
legada mantida abaixo é somente documentação histórica e não autoriza execução.
O write-mode revalida cada ID esperado contra admissão, quarentena, cooldown e
estado atual. A entrada de novos posts pode mudar a ordenação da fila, mas não
substitui os IDs autorizados pelo preview; se algum ID esperado deixar de ser
elegível, o write-mode bloqueia sem executar stages.

## Orquestração e auditoria V2

- Preserve `_hermes_work_state`, checkpoints, journals, manifests, tentativas,
  cooldowns e MediaRegistry. Não reconstrua registros sem evidência canônica.
- `completed=1` é apenas conclusão de uma passagem; audite sempre `outcome`,
  `phase`, `blocker`, estado WordPress, journal, Registry e readback.
- `accepted`/`accepted_working` é aquisição intermediária, não aplicação. Só
  `applied` com HTML/featured readback pode contar como mídia aplicada.
- Pare na primeira divergência entre V2, checkpoint, WordPress, Registry ou lock.
- Publisher e editorial são contratos separados. O publisher permanece pausado
  até existir ao menos um READY novo com manifesto, hash e mídia confirmados.

## Orçamento da sessão (parar TAMBÉM é o trabalho)

- Meta de produção = `EDITOR_TARGET_READY_PER_RUN` (5 posts READY, distribuída
  entre sessões curtas). TETO de posts tocados = `EDITOR_MAX_POSTS_TOUCHED_PER_RUN`
  (default 5) — hard cap: `skipped`, `uncertain` e `blocked` liberam a vaga de
  READY, mas NÃO o teto de tocados.
- `cards --compact` traz `session{remaining_posts, context_bytes_used}` e corta o
  lote ao que ainda cabe. `count: 0` ou `stop` preenchido => **ENCERRE a sessão**:
  nada de buscar mais cards, nada de "aproveitar para adiantar outro post".
- `apply` de post NOVO acima do teto devolve `status: session_budget_exhausted`
  (nada foi escrito, nenhuma tentativa gasta): encerre a sessão.
- NUNCA simplifique o checklist para caber no orçamento — encerre a sessão.

## Procedimentos legados — somente referência histórica (NÃO EXECUTAR)

> Esta seção não pertence ao lifecycle operacional. Para produção, use somente
> `v2-run` conforme a seção **Executor oficial V2** acima.

Quando houver dois posts novos elegíveis, a antiga estratégia de microbatch era

1. Antes de qualquer escrita, rode o preflight read-only com os dois IDs:

   ```bash
   unicornio-editor canary-preflight 114463 114465 --root .
   ```

   O comando valida as credenciais, confirma `pending`, testa a leitura da
   Media Library e mostra a telemetria das últimas 24h. Se retornar
   `status=blocked`, corrija a causa antes de iniciar o batch.

2. Prepare os posts em envelopes independentes:

   ```bash
   unicornio-editor prepare-batch 114463 114465 --batch-id batch-YYYYMMDD-01 --root .
   ```

   O comando grava `work/batches/<batch_id>/manifest.json`, um
   `post-<id>.json` completo por post e `editorial.input.json`, o envelope único
   da inferência editorial. O stdout é somente o resumo.
3. Leia `editorial.input.json` uma vez e produza o arquivo editorial sem chamar
   ferramentas entre a entrada e a saída. Preferencialmente deixe o Python
   fazer a chamada com `editorial-generate-batch editorial.input.json`; o Hermes
   não deve iniciar um loop de tools para escrever o editorial:

   ```bash
   unicornio-editor editorial-generate-batch \
     work/batches/batch-YYYYMMDD-01/editorial.input.json --root .
   ```

   ```json
   {
     "schema_version": 1,
     "batch_id": "batch-YYYYMMDD-01",
     "results": [
       {"post_id": 114463, "status": "ok", "editorial": {"site_relevance": {}, "media_plan": []}},
       {"post_id": 114465, "status": "ok", "editorial": {"site_relevance": {}, "media_plan": []}}
     ]
   }
   ```

   Todos os IDs devem ser únicos. Um artigo que não possa ser gerado deve sair
   como `status: needs_retry` com `reason`, sem invalidar o outro resultado. O
   código valida o envelope inteiro antes de aplicar o primeiro item.
4. Aplique com:

   ```bash
   unicornio-editor apply-batch editorial-batch.json --compact --root .
   ```

   Cada item passa pelo `apply` normal, em sequência, com seu próprio backup,
   lock, checklist, mídia, manifest SHA-256 e `_hermes_state`. Uma falha fica
   isolada no post; não repita o batch inteiro. Se o orçamento da sessão acabar,
   o comando para antes do próximo post novo e lista os IDs restantes.

5. Para mídia, não peça ao modelo uma busca/validação por post. Gere um único
   envelope determinístico e execute `media-resolve-batch`:

   ```json
   {
     "batch_id": "batch-YYYYMMDD-01",
     "posts": [
       {"post_id": 114463, "subject": "Obra A", "needed": 2, "query": "Obra A"},
       {"post_id": 114465, "subject": "Obra B", "needed": 2, "query": "Obra B"}
     ]
   }
   ```

   O Python faz reuso, descoberta, prova de origem, relevância e pHash sem
   outro turno Hermes. Se `vision_batch_file` for produzido, execute
   `vision-batch` uma vez; ele faz uma chamada low e escala somente os itens
   ambíguos para uma segunda chamada high, nunca o lote inteiro.

Quando dois ou mais posts tiverem featured candidates já escolhidos, a visão
também pode ser amortizada antes do `media-validate`:

```bash
unicornio-editor vision-batch vision-batch.json --root .
```

O contrato usa `items[{candidate_id, post_id, image_url, subject,
require_key_art}]`. Cada resultado é correlacionado pelo `candidate_id`; apenas
MATCH/UNRELATED definitivos entram no cache individual. Resultados ambíguos não
são tratados como aprovação: seguem para a escalada `high` somente dentro do
subconjunto ambíguo.

Não envie para o modelo uma resposta combinada com conteúdo de posts sem
`post_id`. A associação por posição é proibida: o ID é a única chave de
correlação e a telemetria carrega `batch_id`/`batch_stage` quando a execução é
batch.

## Fluxo V2 oficial — referência operacional

A execução real é feita pelo `v2-run` e não pelos procedimentos históricos
abaixo. Estes invariantes devem ser verificados no resultado do runner:

1. `v2-run --preview` antes de qualquer write-mode;
   `images{required,valid,missing,irrelevant,non_webp}`, `featured` (ação),
   `fix`, `requires_content`, `session`. Escreva os editoriais direto dos cards;
   NÃO abra blocked.json/logs/source. Fila geral: `queue --compact`.
2. Para posts NOVOS, agrupe no máximo dois IDs elegíveis em
   `prepare-batch`/`apply-batch`. Rode até a meta de 5 READY ou até o teto de
   tocados / `cards count: 0`. Rework continua preferencialmente individual.
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
   `media-search-listicle "OBRA 1" "OBRA 2" ... --post-id POST_ID --limit 3`.
6. Escolha a imagem: `decision: auto` → use `select` direto. `choose` →
   escolha entre 2-3 `options`. `reuse` → o acervo local já cobre a necessidade
   INTEIRA (não houve busca web); `coverage: mixed` indica cobertura parcial.
   Se `decision: none`/`coverage: none`, nenhuma mídia foi validada: não crie
   estado editorial, não chame `uncertain`, mantenha o item ausente do
   `media_plan` e siga obrigatoriamente até `apply`; após
   `site_relevance=process`, falhas de mídia pertencem ao `PARTIAL`.
   (Media Library/índice local) vem ANTES da web: use-o primeiro. Google Images é
   só índice; a página original é a fonte. Não faça pré-verificação manual.
   **Copie `decision_id` (e `decision`) de cada busca/item para o item
   correspondente do `media_plan`** — é o que liga causalmente a decisão à
   imagem e permite medir a qualidade por decisão (sem isso, o resultado cai na
   última decisão do post).
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
NEW | PROCESSING | BLOCKED | PARTIAL | READY | SKIPPED | UNCERTAIN | AWAITING_HUMAN | PUBLISHED
```

- READY = preflight 100%. SÓ `ready` publica.
- PARTIAL = relevância aprovada, mas mídia/featured incompleta; reutilize o
  manifesto `editorial.partial.json` e busque somente o déficit. PARTIAL nunca
  publica e não altera o HTML do WordPress até checklist 100%.
- BLOCKED = falha de texto, SEO, trailer ou outro gate corrigível; não use
  UNCERTAIN para falta de mídia.
- UNCERTAIN = exclusivamente dúvida editorial sobre relevância. Fica fora da fila
  até `next_retry_at` vencer, aparece uma vez como
  `retry_mode=uncertain_second_pass` e, se continuar incerto, escala para
  `AWAITING_HUMAN`. Nunca publica enquanto estiver `UNCERTAIN`.
- SKIPPED / AWAITING_HUMAN = fora da fila automática e não publica.
- Monitor (`queue --monitor`) só acorda com trabalho elegível; a assinatura é
  estável e muda apenas quando há mudança real na fila ou quando um cooldown expira.

## Rework (verificar → corrigir → publicar)

- Corrija pelo `fix` do card **e só o componente apontado** (`draft --for-fix`);
  altere o mínimo e re-aplique com `--merge-draft`. NUNCA re-aplicar sem correção.
  — nenhuma mídia válida foi encontrada: deixe `media_plan` vazio, execute
  `apply` e permita que o checklist classifique `PARTIAL 0/N`. Nunca use
  `uncertain` para falta de imagem, featured, origem, pHash ou visão.

## Economia de contexto (cron runs — todo token custa dinheiro)

- REUSO PRIMEIRO (mídia): `media-search-web` já devolve `reuse` (Media Library/
  índice local verificado). Só o déficit restante vai à web.
- UMA busca por obra, com `--needed` igual ao que FALTA. Nunca junte obras numa
  query ampla (mistura assuntos); no listicle use o comando em lote.
- `apply --compact` SEMPRE. `media-search-web`/`media-validate`/`cards` já são
  compactos por padrão; o JSON grande fica em `work/**` (auditoria).
- `list-pending --compact` e `prepare --compact` SEMPRE (~1.3 KB vs ~120 KB).
- Para `retry_mode=uncertain_second_pass`, NÃO decida relevância somente pelo
  card: execute `prepare-batch`/`editorial-generate-batch` e use o contexto real
  (título, conteúdo, origem, entidades, motivo anterior e `relevance_policy`).
  Só depois de `site_relevance=process` execute a etapa determinística de mídia;
  mesmo com `media_plan=[]`, execute `apply`.
- Depois de `site_relevance=process`, nunca chame `uncertain` nem
  `awaiting_human` por falta de mídia. O `apply` é a autoridade: mídia incompleta
  vira `PARTIAL`, e somente `no_progress_attempts` do PARTIAL pode escalar para
  `AWAITING_HUMAN`.
- Para posts NEW/BLOCKED, decida skip/uncertain pelo card e pelo fluxo indicado;
  nunca busque mídia antes de confirmar relevância.
  caminho; nunca cole o JSON duas vezes.
- OMITA `cleaned_html` sem reescrita; com mídia nova ele é OBRIGATÓRIO. OMITA
  `seo` quando `seo_exists` e não houver mídia. NUNCA inclua CTA/Fonte no HTML.
- Tópicos: `matched_topics` precisa intersectar SITE_TOPICS. Alt de imagem sempre
  nomeia a obra (nunca genérico).
- Cada imagem do corpo DISTINTA (nunca o mesmo frame em crops/tamanhos
  diferentes); a featured não reaparece no corpo. Bloqueado por
  `imagens_similares`: remova as cópias e siga — o mínimo é dimensionado pelos
  frames distintos reais, NUNCA repita para atingir a cota.
- IMAGENS SÃO OBRIGATÓRIAS (2/4/6 sem waiver). Se a busca honesta não encontrar
  mídia, deixe o plano vazio e execute `apply`: falha exclusivamente de mídia é
  `PARTIAL`, nunca `uncertain`. Jogo sem trailer oficial: nunca use fan-made.
- NUNCA leia `src/**`, `pyproject.toml`, `.env` nem testes — o CLI é a interface.
  Não repita comandos; não re-prepare post já visto.

## Diagnóstico barato (interativo)

- `scripts/diagnostico.sh` (fila + telemetria; read-only; custo < $0.01).
- `unicornio-editor telemetry` resume blocagens/resultados e o contexto por
  comando e por post (`context_bytes_by_command`, `by_post`,
  `context_bytes_per_ready`) distinguindo "não há imagem" de "busca falhou".
  Runs batch aparecem em `batches`, agrupadas por `batch_id`/`batch_stage`.
  `economics` separa requests Hermes/modelo editorial, requests diretos de
  visão, chamadas de ferramenta, HTTP externo e custo.
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
