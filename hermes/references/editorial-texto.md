# Qualidade Editorial de Texto e Listas (UnicornioHater)

> Referência consumida pelo SKILL `unicorniohater-editor` ao REESCREVER texto
> (`cleaned_html`) ou validar estrutura. Antes era o bloco "Texto e SEO" e
> "Listas e rankings" do SKILL.md raiz; unificado aqui em 2026-08.

## Texto e SEO

- Escreva em portugues brasileiro natural.
- Melhore clareza, ritmo, transicoes, escaneabilidade e interesse.
- Contextualize por que a noticia importa, sem inflar importancia.
- Preserve fatos, datas, numeros, nomes, plataformas e incertezas.
- Nunca invente fatos.
- Evite repeticoes e frases genericas de IA.
- Antes de processar, confirme correspondencia entre o assunto, os
  temas/categorias/tipos de conteudo aceitos pelo site e os topicos ja
  publicados; em caso de duvida, pule sem alterar.
- Ate 600 palavras: pelo menos 2 imagens relevantes; de 601 a 1.000: pelo menos
  4; de 1.001 a 1.500: pelo menos 6. Para textos maiores, mantenha pelo menos 6
  e aumente conforme a densidade do conteudo. A quantidade e um minimo
  editorial, nao motivo para inserir imagens sem relacao direta — cada imagem
  deve contextualizar o trecho e passar pela licenca.
- Imagens inline centralizadas (`figure.aligncenter` ou equivalente); a
  featured image segue o wrapper visual do tema sem duplicar a imagem no corpo.
- Revise semanticamente titulo, subtitulos, palavra-chave, entidades e termos
  relacionados; evite keyword stuffing e exija que o foco apareca naturalmente
  no titulo e no corpo.
- Organize textos longos com subtitulos descritivos e paragrafos curtos; o
  resultado deve ser interessante para uma pessoa, nao apenas para um crawler.
- Remova sinais de texto automatizado: travessoes recorrentes (`—`/`–`),
  frases genericas, repeticoes, conclusoes obvias e construcoes mecanicas.
- SEO title <= 65 caracteres. Meta description entre 120 e 160 caracteres.
- Keyword deve aparecer naturalmente; sem stuffing.

## Listas e rankings

- Detecte listas, rankings, tops, selecoes e recomendacoes numeradas pelo
  numero no titulo e pelos termos do formato.
- A quantidade prometida no titulo deve ser exatamente a quantidade de itens
  principais.
- Cada item deve seguir `H2 numerado -> imagem relacionada centralizada ->
  descricao`, sem texto entre o H2 e a imagem.
- O H2 deve conter numero, nome identificavel do item e complemento descritivo;
  a sequencia deve ser crescente ou decrescente, sem duplicatas ou lacunas.
- Imagens genericas ou de outro item nao servem para cumprir a metrica. O
  credito e a licenca continuam obrigatorios.
- Remova sempre a tag `<article>` do conteudo; o tema e responsavel pelos
  wrappers externos.
- Antes de publicar, valide quantidade, numeracao, presenca de H2, imagem
  imediatamente posterior, descricao posterior e consistencia estrutural entre
  todos os itens.
