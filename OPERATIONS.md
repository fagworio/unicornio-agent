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

## Instalar cron Hermes
Copie/linke `hermes/SKILL.md` para a pasta de skills do Hermes com o nome `unicorniohater-editor` e rode:

```bash
./hermes/cron-install.sh
```

Após o install, registre no `.env` o ID do job editorial criado (ou já
existente) para que o relatório não some custos de outros crons:

```env
HERMES_EDITORIAL_CRON_JOB_ID=ID_DO_JOB_EDITORIAL
# Exemplo de teto inicial; 0 deixa o freio desligado.
HERMES_EDITORIAL_DAILY_COST_LIMIT_USD=0.80
```

O monitor é orientado a eventos: ele acorda o agente quando a assinatura da
fila muda, não a cada polling enquanto houver backlog. Para aplicar essa
alteração ao job instalado, execute novamente `./hermes/cron-install.sh`.
O freio usa apenas o custo atribuído ao ID informado no `state.db`; se essa
atribuição não existir na versão instalada do Hermes, ele falha aberto e o
relatório deixa isso explícito, preservando a operação.

## Rollback
Cada `prepare` salva um snapshot JSON completo do post em `backups/` antes do trabalho. O rollback pode ser feito manualmente a partir do `content.raw`, metas e featured media do snapshot.
