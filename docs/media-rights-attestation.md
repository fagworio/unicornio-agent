# Evidência durável de direitos de mídia

O validador assina com HMAC a identidade da evidência de licença e os campos que determinam atribuição: imagem direta, página de origem, autor, licença, URL da licença, captura, crédito e texto alternativo. A chave é criada por instalação em `work/rights-attestation.key`, com 32 bytes e modo `0600`; ela não pertence ao repositório nem deve ser incluída em artefatos públicos.

Ao migrar ou restaurar a mesma instalação, preserve a chave junto com o estado privado de `work/`, mantendo proprietário e permissões restritas. Não copie a chave entre instalações. Se a chave for perdida ou estiver inválida, as assinaturas antigas falham fechadas; o fluxo deve buscar novamente a fonte/API e gerar nova evidência somente após revalidar os direitos atuais. Não recrie a chave para “corrigir” uma assinatura antiga.

A assinatura persistente comprova integridade e identidade do registro originalmente observado; não concede direitos por si só nem substitui a consulta atual à origem. O fluxo editorial reobtém o HTML Schema.org ou os metadados autoritativos do Wikimedia antes do upload. Uma fonte indisponível permanece falha técnica recuperável, sem reutilizar uma assinatura antiga como autorização.

Os testes verificam validação em processo independente, rejeição após mudança da licença, divergência de URL, autor/crédito/alt alterados e permissões privadas da chave. A homologação de aquisição E2E usa um servidor HTTP efêmero com dados sintéticos; a prova CC0 dessa fixture não representa uma licença real ou aprovação de material de terceiros.
