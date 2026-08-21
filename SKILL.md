# UnicornioHater Editorial Agent

## Objetivo
Processar autonomamente posts WordPress com status `pending`, melhorar texto/SEO, enriquecer com midia quando fizer sentido e manter o post em `pending`. Nunca publicar.

## Regras absolutas
- Antes de editar, classificar se o conteudo pertence a linha editorial do UnicornioHater.
- Se o conteudo for irrelevante, estiver no post errado ou a classificacao for incerta, PULAR o post sem alterar nada e seguir para o proximo `pending`.
- Nunca tentar adaptar um assunto irrelevante inventando uma ligacao com games/cultura geek.
- NUNCA mudar status para `publish`, `future`, `private` ou outro status.
- NUNCA chamar endpoint de publicacao fora dos scripts do projeto.
- Processar no maximo o batch configurado.
- Fazer backup antes de editar.
- Se qualquer validacao falhar, nao gravar alteracoes.
- Imagens inline importadas com bloco de crédito COMPLETO na figura (Crédito da imagem: autor. descrição. Licença <CC/CC0/domínio público> + URL da licença) são PRESERVADAS pelo código (clean_html valida deterministicamente) — não redescobrir nem re-uploadar; não entram no media_plan.
- Imagens inline importadas sem crédito completo são removidas; novas imagens só podem vir de candidatos descobertos pelo Google Images cuja licença pública/permissão foi confirmada na página original.
- RELEVÂNCIA DA IMAGEM (obrigatório): a imagem deve representar EXATAMENTE o assunto citado — a obra, personagem, objeto ou pessoa — nunca um conceito genérico que só compartilha palavra-chave. Ex.: post sobre o jogo Redfall (vampiros) exige key art/screenshot do Redfall; foto de morcego real é REJEITADA. O gate de código (media/relevance.py) rejeita candidatos sem sobreposição com as entidades distintas do post (palavras-conceito como vampiro, jogo, anime, convenção nunca contam). Se não houver imagem realmente relacionada, NÃO incluir — ausência é melhor que imagem errada. O checklist (relevancia_imagens) bloqueia publicação com imagem fora de contexto, e o mínimo 2/4/6 é dispensado quando o post fica sem imagens.
- Cada imagem deve ter URL da página original, autor, licença, URL da licença e crédito registrado.
- Baixar para área temporária, validar, converter para WebP e enviar pela Media Library REST local; não usar bucket/CDN externo.
- Crédito visível é obrigatório em toda imagem e deve seguir o formato `Crédito da imagem: Autor/Empresa. Descrição da imagem. Informação de licença ou direitos autorais, quando aplicável.`; crédito não substitui licença.
- Para featured images, salvar o crédito também na legenda do attachment e no conteúdo visível quando o tema não renderizar a legenda.
- URLs locais legadas em `/wp-content/uploads/2019/06/` devem usar o fallback de reupload para uma fonte efetiva e WebP; uploads atuais em `/wp-content/uploads/2025/03/` não precisam desse fallback no ambiente local.
- Sem licença verificável, não usar a imagem.
- A fonte vem exclusivamente do custom field `original_link`.
- Fonte só é adicionada quando `original_link` existe; deve usar target="_blank", rel="nofollow noopener" e ponto antes de `</em>`.
- CTA canonico e inserido pelo codigo, nao pelo modelo.

## Fluxo por execucao
1. Rode `unicornio-editor list-pending --compact` (nunca o modo completo: ele despeja ~120 KB de
   conteudo por batch no contexto; o compacto imprime so id/titulo/palavras/link).
2. Para cada post retornado, rode `unicornio-editor prepare ID`.
3. Classifique a relevancia antes de qualquer reescrita. Se `site_relevance.decision=skip`, gere o JSON de skip, rode `apply` apenas para registrar o resultado local/saida (ele nao altera o WordPress) e passe imediatamente ao proximo post.
4. Se relevante, edite apenas o `cleaned_html` conforme o fluxo editorial. O `cleaned_html` no JSON
   e OPCIONAL: se o texto preparado ja estiver bom, omita (o `apply` reusa o conteudo preparado,
   sem reescrita). So inclua quando reescrever de fato. NUNCA inclua CTA, Fonte ou rodape no
   `cleaned_html` — o codigo insere o CTA+Fonte canonicos e remove duplicados.
5. Retorne/produza JSON estrito no formato exigido pelo prompt editorial.
6. Pesquise imagens apenas quando o `media_plan` indicar ganho real de leitura. Use Google Images para descoberta, mas abra a página original e confirme licença pública, Creative Commons compatível ou permissão explícita.
7. Quando o conteúdo for sobre um jogo, preencha `game_name` no JSON editorial com o nome exato do jogo; a descoberta e a validação do trailer são determinísticas (o código busca no YouTube `<game_name> trailer`, valida via oEmbed e insere o embed antes do CTA) — nunca invente URLs de trailer. Conteúdo que não seja de jogo usa `game_name: null`.
8. Registre para cada imagem a origem, autor, licença, URL da licença, data da captura e texto de crédito. Se não for possível confirmar, descarte.
9. Baixe imagens escolhidas para área temporária, valide MIME/tamanho/resolução, converta para WebP e faça upload pela REST API da Media Library local. Não use bucket ou CDN externo.
10. Insira cada nova imagem entre parágrafos, nunca dentro de parágrafo, imediatamente após H2, antes do CTA ou da fonte. Mantenha ao menos 3 parágrafos de distância quando possível e inclua crédito visível.
11. Imagem de destaque é OBRIGATÓRIA para publicar: marque no `media_plan` o item com `is_featured: true` (no máximo 1). O código garante 1200x720 (cover-crop 5:3) e WebP automaticamente; nunca use thumbnail do YouTube (licença não verificável).
12. Salve o JSON editorial em arquivo e rode `unicornio-editor apply ID arquivo.json`.
13. Antes de qualquer publicacao, rode o checklist sequencial e so publique se TODOS os itens passarem:
    `unicornio-editor checklist ID arquivo.json` — verifica na ordem: backup, status pending,
    relevancia, conteudo, Fonte (original_link), imagens por tamanho (2/4/6), imagem de destaque
    obrigatoria 1200x720, WebP, trailer (se jogo), CTA, qualidade de texto, estrutura, schema. O
    resultado `checklist` tambem vem no JSON do `apply`.
14. Fluxo de publicacao: o `apply` salva `backups/<ID>/editorial.latest.json`; publique somente via
    `unicornio-editor publish ID` ou `publish-ready` (todos os pending prontos). O comando revalida o
    checklist completo e so publica com `PUBLISH_ENABLED=true` (o script do cron de
    publicacao liga PUBLISH_ENABLED; o .env roda com EDITOR_DRY_RUN=false em producao).
    NUNCA publique manualmente por fora do fluxo.
    Plano de publicacao (cron diario, America/Sao_Paulo): 00h=5, 08h=7, 12h=8,
    18h=10, 21h=10 (~40/dia). Backlog novo chega entre 03:30 e 05:00;
    `PUBLISH_LIMIT` define a cota da janela e conta apenas posts publicados de fato.
15. Em producao o projeto roda em write mode (EDITOR_DRY_RUN=false): o `apply` grava de verdade
    (conteudo + meta), sempre forcando status `pending` — a publicacao so acontece via `publish-ready`.

## Economia de tokens (obrigatorio em toda execucao paga)

- `list-pending --compact` e `prepare ID --compact` sempre (o `prepare --compact` grava o JSON
  completo em `backups/<ID>/prepared.json`; leia esse arquivo quando precisar do cleaned_html).
- Nunca leia `src/**` para entender o fluxo; o CLI e a interface. So leia codigo se um erro do CLI
  nao for auto-explicativo.
- Nunca despeje HTML baixado no terminal: salve em `/tmp` e extraia so o trecho (licenca/autor).
- Nao repita comandos; se `list-pending` voltar vazio, encerre imediatamente.
- Escreva o JSON editorial em arquivo e passe o caminho ao `apply`/`checklist`; nao cole o corpo
  do JSON mais de uma vez na conversa.

## Texto e SEO
- Escreva em portugues brasileiro natural.
- Melhore clareza, ritmo, transicoes, escaneabilidade e interesse.
- Contextualize por que a noticia importa, sem inflar importancia.
- Preserve fatos, datas, numeros, nomes, plataformas e incertezas.
- Nunca invente fatos.
- Evite repeticoes e frases genericas de IA.
- Antes de processar, confirme correspondência entre o assunto, os temas/categorias/tipos de conteúdo aceitos pelo site e os tópicos já publicados; em caso de dúvida, pule sem alterar.
- Até 600 palavras: pelo menos 2 imagens relevantes; de 601 a 1.000: pelo menos 4; de 1.001 a 1.500: pelo menos 6. Para textos maiores, mantenha pelo menos 6 e aumente conforme a densidade do conteúdo.
- A quantidade é um mínimo editorial, não motivo para inserir imagens sem relação direta. Cada imagem deve contextualizar o trecho e passar pela licença.
- Imagens inline devem ser centralizadas (`figure.aligncenter` ou equivalente); a featured image deve seguir o wrapper visual do tema sem duplicar a imagem no conteúdo.
- Revise semanticamente título, subtítulos, palavra-chave, entidades e termos relacionados; evite keyword stuffing e exija que o foco apareça naturalmente no título e no corpo.
- Organize textos longos com subtítulos descritivos e parágrafos curtos; o resultado deve ser interessante para uma pessoa, não apenas para um crawler.
- Remova sinais de texto automatizado: travessões recorrentes (`—`/`–`), frases genéricas, repetições, conclusões óbvias e construções mecânicas.
- SEO title <= 65 caracteres.
- Meta description entre 120 e 160 caracteres.
- Keyword deve aparecer naturalmente; sem stuffing.

## Listas e rankings
- Detecte listas, rankings, tops, seleções e recomendações numeradas pelo número no título e pelos termos do formato.
- A quantidade prometida no título deve ser exatamente a quantidade de itens principais.
- Cada item deve seguir `H2 numerado -> imagem relacionada centralizada -> descrição`, sem texto entre o H2 e a imagem.
- O H2 deve conter número, nome identificável do item e complemento descritivo; a sequência deve ser crescente ou decrescente, sem duplicatas ou lacunas.
- Imagens genéricas ou de outro item não servem para cumprir a métrica. O crédito e a licença continuam obrigatórios.
- Remova sempre a tag `<article>` do conteúdo; o tema é responsável pelos wrappers externos.
- Antes de publicar, valide quantidade, numeração, presença de H2, imagem imediatamente posterior, descrição posterior e consistência estrutural entre todos os itens.

## Evolucao
Rotinas de manutencao futura ficam separadas do pipeline `pending`: imagens quebradas, posts antigos, refresh SEO, links quebrados e midia orfa. Inicialmente devem rodar apenas em modo report.
