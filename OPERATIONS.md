# Operacao

## Primeiro deploy

```bash
sudo mkdir -p /opt/unicorniohater-editorial-agent
sudo chown "$USER":"$USER" /opt/unicorniohater-editorial-agent
cp -a . /opt/unicorniohater-editorial-agent/
cd /opt/unicorniohater-editorial-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Edite `.env` e comece com:

```env
EDITOR_DRY_RUN=true
```

## Smoke test

```bash
unicornio-editor list-pending
unicornio-editor prepare ID
```

## Teste de mídia licenciada
A pesquisa parte do Google Images, mas a aprovação depende da página original e da licença verificável. Baixe somente para área temporária, converta para WebP e envie pela Media Library local; não use bucket/CDN externo. Confirme no attachment o crédito e os metadados de licença.

```bash
unicornio-editor upload-image "URL_DA_IMAGEM" \
  --slug teste-offload \
  --alt "Imagem de teste" \
  --title "Teste do agente"
```

Confirme que o attachment apareceu na Media Library local com crédito e licença registrados. Não há dependência de WP Offload Media, bucket ou CDN externo.

## Ativar escrita
Depois de revisar varios dry-runs:

```env
EDITOR_DRY_RUN=false
```

## Admissão do V2 editorial

O cron do V2 só processa posts cuja data original do WordPress seja igual ou
posterior ao marco fixo configurado em `EDITOR_V2_ADMISSION_AFTER`. O filtro é
aplicado antes de cooldown e scheduler; portanto um post admitido continua
normalmente pelos retries de `RELEVANCE`, `EDITORIAL`, `MEDIA` e `VALIDATE`.

Configure o marco com data e fuso explícitos:

```env
EDITOR_V2_ADMISSION_AFTER=2026-10-06T22:30:00-03:00
```

Se a variável estiver ausente ou inválida, a fila fecha com segurança e nenhum
post é selecionado. Posts anteriores permanecem intactos e aparecem no
relatório como `historical_excluded`; não têm cooldown, tentativas ou estado
alterados. O relatório do `v2-run` também expõe `pending_total`,
`admitted_pending`, `eligible_ids`, `cooldown_ids` e
`historical_excluded_ids` para auditoria.

## Discovery de mídia via navegador

O `media-search-web --engine auto` tenta Google Images em Chromium real antes de
Bing/Yandex. O Google é somente índice: `source_page_url` continua obrigatório
e todos os gates determinísticos permanecem ativos. Para instalar o provider em
um host de produção:

```bash
pip install -e '.[browser]'
playwright install chromium
```

Se Playwright/Chromium estiver ausente, houver CAPTCHA, consentimento ou
mudança de DOM, o provider registra `google_browser_unavailable` e o fallback
continua sem tentar contornar o bloqueio. Para um POC somente leitura:

```bash
EDITOR_GOOGLE_BROWSER_ENABLED=true \
unicornio-editor media-search-web "SUBJECT REAL" --engine=google_browser --full
```

Os bytes carregados pelo browser são mantidos localmente durante o fluxo e
reutilizados no download quando disponíveis; a telemetria registra apenas
metadados e `sha256`, nunca Base64.

## Instalar cron Hermes
Copie/linke `hermes/SKILL.md` para a pasta de skills do Hermes com o nome `unicorniohater-editor` e rode:

```bash
./hermes/cron-install.sh
```

O instalador registra automaticamente no `.env` o ID do job editorial criado
(ou já existente), para que o relatório e o teto diário usem atribuição exata.
Confira o valor após a instalação; só o altere manualmente em uma recuperação:

```env
HERMES_EDITORIAL_CRON_JOB_ID=ID_DO_JOB_EDITORIAL
# Teto inicial por 24h; o monitor não acorda o LLM ao atingi-lo.
HERMES_EDITORIAL_DAILY_COST_LIMIT_USD=1.20
```

O monitor é orientado a eventos: ele acorda o agente quando a assinatura da
fila muda, não a cada polling enquanto houver backlog. O instalador usa
`every 2h` por padrão e processa até cinco posts por sessão; para aplicar essa
alteração ao job instalado, execute novamente `./hermes/cron-install.sh`.
O freio prefere o ID informado no `state.db`; em versões sem essa coluna, usa
o diretório do projeto (`cwd`/`git_repo_root`), sem misturar outros crons.

## Rollback
Cada `prepare` salva um snapshot JSON completo do post em `backups/` antes do trabalho. O rollback pode ser feito manualmente a partir do `content.raw`, metas e featured media do snapshot.
