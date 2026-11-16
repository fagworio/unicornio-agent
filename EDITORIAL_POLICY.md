# Politica editorial do agente

## O que pode mudar
- ortografia, concordancia, pontuacao e construcoes truncadas;
- ordem de frases quando isso melhora clareza sem mudar o sentido;
- paragrafos longos ou curtos demais;
- repeticoes;
- introducao excessivamente lenta;
- H2 genericos ou pouco informativos;
- transicoes entre secoes;
- SEO title, meta description e focus keyword;
- midia ilustrativa e trailer oficial quando agregam valor.

## O que nao pode mudar sem fonte adicional confiavel
- datas;
- precos;
- plataformas;
- nomes proprios;
- numeros;
- citacoes;
- status de anuncio/rumor/confirmacao;
- autoria de declaracoes;
- especificacoes tecnicas;
- contexto factual ausente no texto.

## Engajamento
Engajamento significa reduzir atrito de leitura e aumentar utilidade, nao fabricar emocao. Prioridades:
1. fato principal cedo;
2. paragrafos com uma ideia central;
3. H2 que antecipam o que o leitor vai encontrar;
4. contexto suficiente para entender por que o fato importa;
5. imagens que interrompem blocos longos e ilustram o assunto adjacente;
6. trailer quando e parte real da noticia;
7. CTA somente no template padrao do site.

## Imagens: decisão editorial e controles técnicos

A política de mídia é selecionada por `MEDIA_SOURCE_POLICY` (`credit_only` no
loader de produção; `strict` como compatibilidade explícita). Nenhum modo
verifica ou atesta licença, autorização, titularidade ou permissão legal. A
responsabilidade editorial pelo uso é do site.

No modo `credit_only`, o pipeline exige URL direta HTTP(S) válida e crédito
visível; não exige `source_page_url` e não bloqueia a candidata porque essa
página não existe ou não lista a variante direta. Se o crédito vier vazio, o
código gera `Crédito da imagem: domínio de hospedagem: <host>.` — o host é
identificado como hospedagem, nunca como autor ou fonte verificada. O plano e
o relatório registram `provenance_mode=credit_only`,
`source_page_verified=false` e `rights_verified=false`.

Relevância continua sendo uma validação independente: query de busca sozinha
não aprova a imagem; nome do arquivo, ALT/crédito e contexto específico devem
corresponder à obra ou item. URLs inseguras, bytes inválidos, baixa qualidade,
dimensões incompatíveis, imagens irrelevantes ou duplicadas continuam
bloqueadas. A mídia nova é convertida para WebP e enviada à Media Library local
do WordPress; o HTML, a destacada, o checklist, o estado READY e o readback
continuam exigidos.

No modo `strict`, página-fonte e URL direta válidas continuam obrigatórias e a
presença/identidade técnica do asset na página é verificada. Créditos
editoriais já existentes são preservados. Campos jurídicos históricos são
ignorados e nunca são reescritos por compatibilidade.

## SEO
- uma keyword principal;
- variações semânticas naturais podem aparecer no texto;
- não repetir keyword para elevar score;
- title deve priorizar clareza e intenção de busca;
- description resume a notícia e oferece motivo real para clicar;
- headings devem servir ao leitor;
- alt text descreve a imagem, não é depósito de keyword.
