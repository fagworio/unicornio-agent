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
- Imagens inline importadas existentes devem ser removidas; não reutilizá-las.
- Novas imagens só podem vir de candidatos descobertos pelo Google Images cuja licença pública/permissão foi confirmada na página original.
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
1. Rode `unicornio-editor list-pending`.
2. Para cada post retornado, rode `unicornio-editor prepare ID`.
3. Classifique a relevancia antes de qualquer reescrita. Se `site_relevance.decision=skip`, gere o JSON de skip, rode `apply` apenas para registrar o resultado local/saida (ele nao altera o WordPress) e passe imediatamente ao proximo post.
4. Se relevante, edite apenas o `cleaned_html` conforme `src/unicornio_editor/prompts.py`.
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
    relevancia, conteudo, Fonte (original_link), imagens no corpo (2/4/6 por tamanho), imagem
    de destaque (obrigatoria), WebP, trailer (se jogo), CTA, qualidade de texto, estrutura e
    schema. O resultado `checklist` tambem vem no JSON do `apply`.
14. Se o projeto estiver em dry-run, apenas reporte. Se não estiver, o script ainda força status `pending`.

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
