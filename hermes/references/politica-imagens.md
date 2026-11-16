# Política técnica de imagens (UnicornioHater)

Esta referência descreve os controles técnicos e editoriais automatizados do
pipeline. O software não verifica, classifica nem atesta licença, autorização,
titularidade ou permissão legal de uso. A escolha e a autorização para uso de
um arquivo externo são responsabilidade do operador/editor, conforme as
políticas e orientações aplicáveis fora do pipeline.

## Descoberta, política de crédito e relevância

A política efetiva vem de `MEDIA_SOURCE_POLICY`: `credit_only` é o default do
loader de produção; `strict` mantém o contrato histórico de página-fonte.
Nenhuma das políticas atesta licença, autorização, titularidade ou permissão
legal. A decisão de uso pertence ao operador/editor.

- Em `credit_only`, cada mídia nova exige URL direta HTTP(S) válida e crédito
  visível. `source_page_url` é opcional, descartada do plano ativo e nunca é
  tratada como origem verificada. Se o crédito faltar, o código gera
  `Crédito da imagem: domínio de hospedagem: <host>.` — isso identifica apenas
  o host do URL e não afirma autoria, fonte editorial nem licença.
- O manifesto e o relatório registram `provenance_mode=credit_only`,
  `source_page_verified=false` e `rights_verified=false`. A ausência de página
  não pode virar `manifest_invalid`, `provider_error` ou bloqueio de preflight.
- Em `strict`, URL direta e página-fonte válida continuam obrigatórias; o
  resolver verifica a presença do asset na página e a identidade dos bytes.
  Se quiser selecionar esse modo explicitamente, configure
  `MEDIA_SOURCE_POLICY=strict`.
- A relevância continua sendo avaliada: query de busca sozinha não aprova
  imagem. O asset precisa corresponder ao assunto por evidência do próprio
  arquivo/crédito; imagens irrelevantes, duplicadas ou tecnicamente inválidas
  continuam rejeitadas. Os gates de URL segura, integridade, qualidade,
  dimensões, WebP e visão aplicável não são dispensados.
- A imagem destacada deve retratar a obra citada; cada imagem inline deve
  corresponder ao item/seção e ser visualmente distinta.

## Requisitos editoriais e técnicos preservados

- Mínimo editorial de imagens: 2 até 600 palavras, 4 até 1.000, 6 acima;
  listicle exige uma imagem inline distinta por item, com mínimo de 2. A
  featured não entra na contagem. Waivers existentes permanecem sujeitos aos
  gates já implementados.
- A busca deve ser honesta. Não declare estratégia esgotada quando houver
  falha técnica ou provider não verificado; preserve os estados de conclusão,
  falha e orçamento da busca.
- ALT descritivo e contexto editorial continuam necessários. Crédito é
  obrigatório no modo `credit_only`; sua ausência é preenchida pelo host do URL,
  explicitamente rotulado como domínio de hospedagem. Crédito nunca comprova
  origem, autorização ou relevância.
- Imagens incompatíveis com o contexto, arquivos inválidos, URLs inseguras,
  duplicatas ou conteúdo de baixa qualidade continuam bloqueados.
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
