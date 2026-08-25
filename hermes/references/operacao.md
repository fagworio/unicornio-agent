# Operação em Produção (UnicornioHater)

> Referência de pitfalls de produção consumida pelo SKILL `unicorniohater-editor`
> quando algo falha ou parece estranho. Consulte ANTES de explorar código.

## Interface (economia de tokens)

- O CLI é a interface. **NUNCA** leia `src/**`, `pyproject.toml`, `.env`,
  `backups/**`, logs ou JSONs grandes para "entender o fluxo". Exceção: erro
  não autoexplicativo -> leia SÓ a função do traceback.
- Para diagnosticar (fila, publicações, custo, crons, tokens): rode UMA chamada
  de `unicornio-editor queue` / `list-pending --compact` (ou `scripts/diagnostico.sh`).
  Custo < $0.01 por verificação. **Não** explore state.db manualmente.

## Ambiente e execução

- O CLI lê env direto: sempre `set -a && . ./.env && set +a && .venv/bin/...`
  (sem o env ele cai na URL mock e "time out").
- `list-pending` consulta status=pending no servidor (filtro local retorna []).

## Janela de publicação (não confundir com cron quebrado)

- **JANELA DE PUBLICAÇÃO SILENCIOSA** = candidatos bloqueados pelo checklist
  (não é o cron quebrado). Diagnostique com `unicornio-editor queue` (estados)
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

## Segurança

- Nunca envie `status` num payload de update. Re-fetch o post imediatamente
  antes de qualquer escrita; aborte se não for `pending`.
- Nunca logue credenciais, tokens, cookies ou headers de autorização completos.
- Nunca use posts de produção para validação local.
