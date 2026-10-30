# Operacao

## Primeiro deploy

```bash
sudo mkdir -p /opt/unicorniohater-editorial-agent
sudo chown "$USER":"$USER" /opt/unicorniohater-editorial-agent
cp -a . /opt/unicorniohater-editorial-agent/
cd /opt/unicorniohater-editorial-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[media-search]'
cp .env.example .env
```

### Estado de publicação por janela

`work/publish-windows/<window_id>.json` é histórico operacional imutável de
cada janela do Publisher. Backups, snapshots, restaurações e deploys devem
preservar esse diretório junto com `work/publish-window.log`, que permanece
somente como compatibilidade para a última janela. Nunca limpar ou substituir
esses relatórios sem preservar os arquivos históricos.

Edite `.env` e comece com:

```env
EDITOR_DRY_RUN=true
```

## Smoke test

### Paridade do ambiente de testes

Os testes de `source_verify` dependem dos codecs e das versões declaradas no
projeto. Execute-os em um ambiente virtual, especialmente em hosts Debian que
bloqueiam instalação global via PEP 668:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q tests/test_source_verify.py tests/test_source_verify_fase5.py
```

Não use `pip install --user` no Python do sistema. Se `ImageHash` estiver
ausente, os testes de recompressão visual falham fechados por projeto; isso é
diferença de ambiente, não autorização para relaxar a verificação de origem.

```bash
unicornio-editor list-pending
unicornio-editor prepare ID
```

## Teste de mídia licenciada
A pesquisa usa o plano de providers configurado, mas o upload só ocorre quando a página original declara um Schema.org `ImageObject` com o `contentUrl` exato e a licença compatível. Páginas sem essa evidência são rejeitadas, mesmo que o plano do agente declare CC. Baixe somente para área temporária, converta para WebP e envie pela Media Library local; não use bucket/CDN externo. Confirme no attachment o crédito e os metadados de licença.

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

Para a execução contínua, use `v2-cycle`. Ele faz preview e write-mode pinados
por rodada, mantém o lock durante o ciclo e grava `work/v2-cycle/state.json` e
`journal.jsonl`. Uma execução interrompida é retomada automaticamente; não use
`--fresh` para descartar um checkpoint `running`. O relatório final inclui
`ready_count`, `unique_posts`, `attempts_total`, `rounds`, `phase`,
`drain_rounds`, `elapsed_seconds` e `stop_reason`. Após estagnação, a fase
`DRAINING` fecha novas admissões e tenta somente retries elegíveis até
`EDITOR_V2_CYCLE_MAX_DRAIN_ROUNDS`.
Qualquer retry editorial que não pertença a `seen_post_ids` consome uma
admissão nova; portanto não entra durante `DRAINING`. Se uma autorização
pendente antiga contrariar esse contrato e não houver journal nem evidência de
write no readback, o ciclo registra `round_authorization_invalidated`, remove
somente a pendência e preserva o histórico das rodadas anteriores. Com
evidência parcial, a retomada permanece bloqueada para reconciliação por post.
Uma reconciliação só aceita journal com o mesmo `cycle_id` e `cycle_round` da
autorização pendente; registros de outro ciclo nunca são prova suficiente.
Se o journal válido comprovar que uma autorização inválida chegou a executar,
o runtime registra `round_reconciled_out_of_budget`, preserva os detalhes por
post e limpa somente a pendência. Essa anomalia não incrementa `rounds`,
`seen_post_ids` ou `attempts_by_post`, nem autoriza novo write.
Antes de invalidar, o runtime verifica também o status WordPress e o
MediaRegistry em modo somente leitura.
O limite de duração conta apenas o tempo de execução das sessões, não o
intervalo em que o checkpoint ficou pausado entre invocações.

### Quarentena operacional por post

`config/v2-quarantine.json` é uma política obrigatória para o scheduler. Um
arquivo ausente ou inválido bloqueia a seleção de todos os posts (o preview
expõe `quarantine_policy_error`). A entrada atual mantém o post `115152` fora
do cron e também bloqueia `v2-run --post-id 115152`, porque os marcadores de
reentrada disponíveis não comprovam autoria humana. O preview mostra
`request_reason=quarantined` e o motivo registrado sem alterar lifecycle,
tentativas ou cooldown.

Para retirar uma quarentena, use uma alteração revisada da política: remova a
entrada ativa e acrescente em `release_history` o `post_id`, `authorization_id`,
`authorized_by` e o motivo da liberação. A revisão deve conferir a autorização
em fonte independente; os campos do arquivo são trilha declarativa, não prova
criptográfica de identidade. Não edite a política diretamente em produção nem
reative crons antes de verificar o preview individual e a aprovação humana.

## Estratégia de busca de mídia

O modo `auto` tenta `duckduckgo,bing,yandex,google` nessa ordem. O pacote
opcional `ddgs` é instalado por `pip install -e '.[media-search]'`; se estiver
ausente, o relatório registra `provider_unavailable` e continua nos fallbacks.
Por padrão, `EDITOR_MEDIA_SEARCH_REQUIRED_PROVIDERS=bing,yandex`: somente esses
providers obrigatórios, quando executados e verificados, comprovam
`EXHAUSTED_VERIFIED`. Falhas em DuckDuckGo ou Google HTML ficam visíveis, mas
não invalidam o plano obrigatório. Uma falha em Bing/Yandex, inclusive
`js_required`, CAPTCHA, cooldown ou erro técnico, impede essa prova. Não há
tentativa de contornar desafios ou política de acesso. Google Browser/Playwright
não é suportado. DDGS é um adapter não oficial sujeito a mudanças; não se deve
tratar suas respostas como fonte independente do Bing sem medir a sobreposição
e o ganho de imagens distintas aprovadas.
O adapter usa o bundle PEM legível do OpenSSL por padrão; para uma CA específica,
defina `EDITOR_DDGS_CA_BUNDLE` para um arquivo legível. Não altere permissões
globais de certificados nem desative a verificação TLS. Respostas não-200, como
`202`, são falhas técnicas e não justificam `EXHAUSTED_VERIFIED`.
O diagnóstico DDGS distingue `vqd_bootstrap` de `image_search` e registra
somente o rótulo do estágio e o status HTTP (sem URL, token ou corpo). Um `202`
isolado não confirma CAPTCHA/proteção antibot; não há retry imediato nem
cooldown automático baseado apenas nesse status.

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

## Recuperação administrativa de ciclo V2 bloqueado

`v2-recover-cycle` é separado de `v2-cycle-abandon`. Ele só aceita um
checkpoint `running` em `ADMISSION`, exige a seleção e a autorização exatas,
exige uma classificação individual para todos os posts selecionados e não faz
writes no WordPress. Posts com efeitos incertos são arquivados por hash e
adicionados à quarentena; posts comprovadamente não executados permanecem
aptos para um ciclo futuro. Journals, sidecars, manifestos, backups, batches e
MediaRegistry são copiados sem alteração. Não use este comando para fabricar
`NOT_EXECUTED` nem para contornar evidência de mídia.

Sempre execute primeiro o preview. O `--apply` requer autorização operacional
explícita e deve ser executado somente após readback do preview:

```bash
unicornio-editor v2-recover-cycle \
  --root /caminho/do/repo \
  --cycle-id CICLO_ATUAL \
  --authorization-id AUTORIZACAO_DA_RODADA \
  --pending-round 3 \
  --selected-ids 115890,115892,115894,115864,115866 \
  --uncertain-post-ids 115890,115892,115894,115864 \
  --not-executed-post-ids 115866 \
  --run-ids 115890=RUN_A,115892=RUN_B,115894=RUN_C,115864=RUN_D \
  --operator-authorization TICKET_AUDITAVEL \
  --new-cycle-id NOVO_CICLO
```

A saída deve declarar `wordpress_writes: 0` e `replay: false`. Não execute
`--fresh`, Writer, upload ou retry como parte desta transição.
