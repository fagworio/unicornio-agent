# Homologação E2E isolada do WordPress Writer V2

O workflow WordPress Writer V2 E2E é uma alternativa oficialmente suportada ao Devilbox local para homologar integrações WordPress. Ele executa em runner Ubuntu e cria WordPress, MySQL, usuário administrador e Application Password locais à execução. Nenhuma credencial, URL, banco de dados, cron ou serviço de produção é usado.

O job é separado da suíte unitária e roda em pull requests destinados a main, em pushes para main e por workflow_dispatch. Em pull requests, faz checkout do SHA exato do head do PR. Após a integração, o evento push homologa novamente o SHA resultante de main; a evidência do PR não é reutilizada para outro commit/tree.

## Cenários obrigatórios

Os testes usam WordPressClient e WordPressWriterV2 reais sobre REST autenticada. O WP-CLI instala/configura o site e a estrutura de rewrite da REST, semeia os posts e cria a autenticação efêmera. O workflow instala os MU-plugins de estado/status existentes em wp/mu-plugins/ no WordPress descartável e acrescenta um MU-plugin restrito a fault injection e auditoria HTTP dos testes. As imagens são fixadas por digest e os digests usados entram no relatório.

1. pending para awaiting_human, com metadados persistidos e readback.
2. awaiting_human sem divergência, sem POST redundante.
3. Metadados divergentes em awaiting_human, com mudança confirmada e sem repetir o POST de status.
4. Idempotência em chamadas reais repetidas.
5. Falha HTTP controlada no POST de status.
6. Interrupção após POST aceito e antes do GET de readback.
7. Resposta de readback divergente, sem falsa confirmação.
8. Aceitação de journal histórico committed sem o campo legado writer_started, validado junto a post real do ambiente.
9. Coerência do MediaRegistry V5 com o resultado confirmado do Writer.

O relatório registra snapshots do WordPress antes/depois, eventos REST sanitizados, resumo dos journals, linhas relevantes do Registry, SHA completo, tree SHA, versão do WordPress, PHP, WP-CLI, imagens e resultado da suíte completa. Não inclui Application Password, senha do administrador ou credenciais do banco.

## Homologação e artefato

O workflow publica wordpress-writer-v2-e2e-<run-id>-<attempt> por 90 dias. classification só será E2E_HOMOLOGADO se os nove cenários passarem sem skip, a suíte completa passar, compilação e identidade SHA esperada forem confirmadas e containers/volumes efêmeros forem removidos. Qualquer falha, evidência incompleta ou limpeza não confirmada resulta em E2E_NAO_HOMOLOGADO e falha do job.

Para executar manualmente, use a aba Actions e escolha WordPress Writer V2 E2E → Run workflow. O relatório é para o SHA indicado em tested_sha e tree_sha; não deve ser tratado como homologação de uma árvore diferente.

O orquestrador local também pode ser executado com python scripts/wordpress_e2e.py, desde que Docker/Compose estejam disponíveis. Ele usa as mesmas imagens e remove o projeto/volume próprio no finally. O checkout original e seus dados não são usados como destino do WordPress.

## Limites operacionais

Esta homologação não executa o pipeline editorial completo, uploads de mídia ou publicação; valida o contrato transacional do Writer com post em revisão humana e o Registry de observabilidade. Não implanta código, não altera agendamentos e não substitui a validação supervisionada de produção. Nenhum gate editorial ou de publicação é relaxado.
