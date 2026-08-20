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
7. Pesquise trailer somente quando `needs_trailer=true`; prefira canal oficial.
8. Registre para cada imagem a origem, autor, licença, URL da licença, data da captura e texto de crédito. Se não for possível confirmar, descarte.
9. Baixe imagens escolhidas para área temporária, valide MIME/tamanho/resolução, converta para WebP e faça upload pela REST API da Media Library local. Não use bucket ou CDN externo.
10. Insira cada nova imagem entre parágrafos, nunca dentro de parágrafo, imediatamente após H2, antes do CTA ou da fonte. Mantenha ao menos 3 parágrafos de distância quando possível e inclua crédito visível.
11. Defina imagem destacada se `featured_media` estiver vazio, usando imagem diferente ou apropriada para capa e com crédito.
12. Salve o JSON editorial em arquivo e rode `unicornio-editor apply ID arquivo.json`.
13. Se o projeto estiver em dry-run, apenas reporte. Se não estiver, o script ainda força status `pending`.

## Texto e SEO
- Escreva em portugues brasileiro natural.
- Melhore clareza, ritmo, transicoes, escaneabilidade e interesse.
- Contextualize por que a noticia importa, sem inflar importancia.
- Preserve fatos, datas, numeros, nomes, plataformas e incertezas.
- Nunca invente fatos.
- Evite repeticoes e frases genericas de IA.
- SEO title <= 65 caracteres.
- Meta description entre 120 e 160 caracteres.
- Keyword deve aparecer naturalmente; sem stuffing.

## Evolucao
Rotinas de manutencao futura ficam separadas do pipeline `pending`: imagens quebradas, posts antigos, refresh SEO, links quebrados e midia orfa. Inicialmente devem rodar apenas em modo report.
