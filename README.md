# UnicornioHater Editorial Agent

Automacao editorial para WordPress acionada pelo cron do Hermes Agent.

## Objetivo

Processar apenas posts `pending`, corrigir estrutura HTML, melhorar texto, preparar SEO, remover imagens inline importadas, descobrir imagens públicas/licenciadas via Google Images, baixar e publicar cópia local em WebP com crédito obrigatório em toda imagem, adicionar imagem destacada/trailer quando fizer sentido, reconstruir CTA/fonte e manter o post em `pending`.

O sistema usa duas camadas:

1. **Hermes/LLM:** julgamento editorial, melhoria de texto, SEO, decisao sobre imagens e trailer.
2. **Codigo deterministico:** WordPress REST, backups, sanitizacao, CTA, fonte, WebP, limites, validacao e bloqueio de publicacao.

## Ambiente de desenvolvimento e testes

Antes de considerar qualquer alteração concluída, valide a implementação no ambiente local do WordPress utilizando o **Devilbox**.

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

Todas as alterações relacionadas ao WordPress devem ser testadas nessa instalação antes de serem consideradas prontas.

### Cenários de teste

Para cada modificação realizada:

1. Identifique quais tipos de posts e quais estados de dados são afetados pela mudança.
2. Crie ou ajuste **posts locais de teste** dentro dessa instalação do WordPress com o status *pending* para reproduzir os cenários necessários.
3. Utilize esses posts locais para testar o comportamento antes e depois da alteração.
4. Inclua cenários positivos, negativos e casos de borda relevantes.
5. Quando a lógica depender de campos, taxonomias, metadados, conteúdo, status ou outras características do post, configure os posts locais com combinações representativas desses dados.
6. Não dependa de posts do ambiente de produção para validar a implementação.
7. Não considere a tarefa concluída apenas porque o código foi alterado: execute os testes no WordPress local e confirme que o resultado obtido corresponde ao comportamento esperado.
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

```text
Hermes cron (monitor: so acorda com trabalho elegivel)
  -> cards (UMA chamada, max 2 posts): delta exato de imagens
     (required/valid/missing/irrelevant/non_webp), diagnostico da featured
     (exists/relevant/webp/dimensions/action) e plano fix para rework
  -> editorial JSON estrito (draft salvo pelo apply ANTES da execucao pesada)
  -> media-validate (1 chamada) quando ha midia nova
  -> apply = PREFLIGHT COMPLETO:
     resolve editorial -> executa midia (download, WebP, upload, credito)
     -> normaliza tecnicamente (featured 1280x720 WebP; inline nao-WebP
        relevante convertida — sem LLM) -> monta conteudo (trailer + CTA + Fonte)
     -> checklist INTEIRO (backup, pending, relevancia, Fonte, 2/4/6, featured,
        WebP, dimensoes, trailer, CTA, qualidade de texto, estrutura, schema)
     -> falha: needs_rework + estado blocked (backoff 30m/2h; 3a falha
        AWAITING_HUMAN) — nada e gravado
     -> sucesso: grava conteudo + meta _hermes_state=ready + Ready Manifest
        (SHA-256) — SOMENTE ready significa apto a publicar
  -> estados no WordPress (_hermes_state/_hermes_attempts/_hermes_next_retry_at/
     _hermes_last_error/_hermes_ready_hash/_hermes_policy_version)
  -> publicacao (cron, 00h=5/08h=7/12h=8/18h=10/21h=10, ~40/dia):
     publish-ready consulta SOMENTE READY; hash do manifest intacto -> publica
     sem revalidar; mudou (STALE) -> revalida com o checklist; falhou -> blocked
     (volta para rework do agente)
  -> qualidade garantida por codigo, nao por diligencia do LLM: o apply nunca
     grava post que o publish bloquearia; o publish apenas confirma
```

## Instalacao

```bash
cd /opt/unicorniohater-editorial-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

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
unicornio-editor maintenance-report posts.json
```

A manutenção é somente relatório e nunca atualiza o WordPress. Para testes locais do Devilbox, consulte `tests/wordpress/README.md`.

## Hermes

Copie ou linke `hermes/SKILL.md` para a pasta de skills do Hermes usando o nome `unicorniohater-editor`.

Depois instale o cron a partir da raiz do projeto:

```bash
./hermes/cron-install.sh /opt/unicorniohater-editorial-agent
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
- Inserir apos um paragrafo que introduza visualmente o assunto, normalmente apos 2-4 paragrafos.
- Minimo de 3 paragrafos entre imagens.
- Converter para WebP antes do upload (normalizacao tecnica e automatica no apply).
- Google Images é somente índice de descoberta; a fonte é a página ORIGINAL da imagem (preview do Google não é fonte).
- Política de imagens (2026-08): qualquer imagem da web pode ser usada com CRÉDITO VISÍVEL — o crédito é a evidência. Licenças livres (CC0, CC BY, domínio público, permissão) são preferidas; para as demais, usar o marcador "Uso com crédito".
- Não usar imagem apenas porque aparece como “pública” no Google; registrar URL da página original, autor, licença (ou "Uso com crédito"), termos e data da captura.
- Baixar e hospedar a cópia na Media Library do WordPress, sem bucket/CDN externo.
- Inserir crédito visível com autor, origem e licença; o crédito é obrigatório em toda imagem.
- Sem crédito visível, descartar a imagem e procurar outra.

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

### Fase 3 - Mídia pública/licenciada
Pesquisa candidatos via Google Images, verifica a licença na página original, baixa, converte para WebP, envia à Media Library local, registra crédito/licença e insere imagens e featured media.

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

## Pontos a integrar na proxima iteracao

1. Adaptador do plugin SEO em uso (Yoast, Rank Math etc.) para gravar title/description/keyword nos metas corretos.
2. Adaptador de pesquisa de imagens com fontes permitidas.
3. Insercao automatica de imagens conforme `media_plan`.
4. Registro e validação da licença/crédito de cada mídia, sem declarar que “Google Images” garante direitos.
5. Marcadores de processamento (`_ai_editor_*`) registrados/expostos na REST API do WordPress.
