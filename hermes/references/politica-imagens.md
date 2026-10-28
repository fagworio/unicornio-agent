# Política técnica de imagens (UnicornioHater)

Esta referência descreve os controles técnicos e editoriais automatizados do
pipeline. O software não verifica, classifica nem atesta licença, autorização,
titularidade ou permissão legal de uso. A escolha e a autorização para uso de
um arquivo externo são responsabilidade do operador/editor, conforme as
políticas e orientações aplicáveis fora do pipeline.

## Descoberta e origem técnica

- Provedores de busca são índices de descoberta, não a origem do arquivo. Use
  a página original e uma URL direta do asset; preview, thumbnail e URL do
  buscador não comprovam a identidade do conteúdo.
- O pipeline verifica que a página declara tecnicamente o asset e compara os
  bytes baixados com os recursos listados. Divergência, página indisponível,
  URL insegura ou evidência inconclusiva continuam bloqueando a candidata.
- Não infira identidade pela legenda, pelo texto do agente ou pelo nome da
  busca. A relevância para o assunto e para a seção é verificada separadamente.
- A featured deve retratar a obra citada. Arte genérica, card de notícia ou
  banner tipográfico não substitui key art apropriada. Cada imagem inline deve
  corresponder ao item/seção e ser visualmente distinta das demais.
- Reuso da Media Library continua sujeito aos mesmos controles técnicos de
  proveniência, assunto, dimensões, integridade e deduplicação. Campos legais
  históricos em planos e registros antigos são ignorados nas decisões atuais.

## Requisitos editoriais e técnicos preservados

- Mínimo editorial de imagens: 2 até 600 palavras, 4 até 1.000, 6 acima;
  listicle exige uma imagem inline distinta por item, com mínimo de 2. A
  featured não entra na contagem. Waivers existentes permanecem sujeitos aos
  gates já implementados.
- A busca deve ser honesta. Não declare estratégia esgotada quando houver
  falha técnica ou provider não verificado; preserve os estados de conclusão,
  falha e orçamento da busca.
- ALT descritivo e contexto editorial continuam necessários. Legenda/crédito
  é opcional: não é prova de origem, autorização ou relevância e sua ausência
  não bloqueia upload, reuso, composição ou `READY`.
- A URL da página de origem e a URL direta do asset são obrigatórias para mídia
  nova. O apply revalida a origem, a identidade do asset e as condições
  técnicas antes do upload.
- A mídia publicada continua sendo baixada por caminho remoto seguro,
  convertida para WebP, achatada quando há transparência e validada quanto a
  dimensões e conteúdo. O upload fica na Media Library do WordPress.
- Imagens duplicadas, irrelevantes, de baixa qualidade, corrompidas,
  inacessíveis ou incompatíveis com o contexto continuam rejeitadas. Falha de
  uma candidata não deve impedir a avaliação independente das demais.
- Posição no texto, espaçamento, estrutura de listicle, integridade do HTML,
  checklist, transição para READY e readback WordPress permanecem inalterados.

## Compatibilidade histórica

Planos/checkpoints antigos podem conter `author`, `license`, `license_url`,
`captured_at`, `rights_verified` ou `source_rights_attestation`. Esses campos
são aceitos somente para leitura compatível e não são usados para autorizar,
rejeitar ou revalidar uma imagem. A normalização do plano ativo os descarta;
os registros históricos não são reescritos nem apagados por esta mudança.

Não preencha metadados legais para tentar satisfazer um gate. Descreva a imagem
com ALT factual e, se útil, uma legenda editorial opcional. O pipeline valida
somente os contratos técnicos e editoriais explicitados acima; não fornece
conclusão sobre direitos de uso.
