# UnicornioHater Editorial Agent — Backlog de Implementação

> Documento criado após inspeção da estrutura atual. O repositório ainda não contém código executável; as tarefas abaixo formam a fundação do produto.

**Objetivo:** construir um agente editorial seguro para processar somente posts WordPress `pending`, gerar revisão/SEO/mídia em modo dry-run e, depois de validado, atualizar o post mantendo o status `pending`.

**Arquitetura:** CLI Python com camadas separadas para configuração, cliente REST WordPress, snapshots/rollback, sanitização HTML, validação, aplicação idempotente e integrações de mídia/SEO. O Hermes/LLM produz um JSON editorial estrito; o código determinístico valida, monta CTA/fonte e decide se pode gravar.

**Ambiente:** WordPress local em Devilbox (`/home/joaofagner/workfolder/devilbox/data/www/wordpress/htdocs`). Foi confirmado que o compose declara `bind`, `php`, `httpd` e `mysql`, mas no momento somente `bind` está em execução.

---

## Diagnóstico da estrutura atual

- Existem somente seis documentos: `README.md`, `IDEA.md`, `ARCHITECTURE.md`, `OPERATIONS.md`, `EDITORIAL_POLICY.md` e `SKILL.md`.
- Não existem `pyproject.toml`, pacote `src/`, testes, `.env.example`, scripts Hermes, diretório `backups/` ou configuração de CI.
- A raiz não é um repositório Git (`git rev-parse --show-toplevel` falhou).
- O WordPress local possui Rank Math (free/pro), WP Offload Media (`amazon-s3-and-cloudfront`) e um plugin local `wp-static-engine`; isso confirma as integrações descritas, mas não prova que estejam ativas/configuradas.
- Há inconsistência de manutenção documental: `README.md` e `IDEA.md` são essencialmente duplicados, e comandos/paths documentados ainda não existem.

## Tarefas priorizadas

### P0 — Fundação executável

1. **Inicializar o projeto Python e CLI**
   - Criar `pyproject.toml`, `src/unicornio_editor/`, entry point `unicornio-editor`, `.env.example`, `.gitignore` e `tests/`.
   - Configurar Python suportado, dependências mínimas, lint/teste e instalação editável.
   - Critério: `python -m pip install -e .` e `unicornio-editor --help` funcionam sem segredo real.

2. **Configuração segura e modo dry-run fail-closed**
   - Criar `src/unicornio_editor/config.py` com validação de URL, credenciais, batch limit, timeout e `EDITOR_DRY_RUN`.
   - Recusar escrita quando dry-run não estiver explicitamente desativado e nunca registrar senha/token.
   - Testar valores ausentes, booleanos inválidos, timeout/limites fora da faixa e logs redigidos.

3. **Cliente WordPress REST com contratos explícitos**
   - Criar `src/unicornio_editor/wordpress.py` para listar apenas `status=pending`, obter post, atualizar post e consultar mídia/meta.
   - Usar timeout, autenticação por Application Password via ambiente, tratamento de HTTP 401/403/404/429/5xx e payload mínimo.
   - Nunca enviar `status` no update; reconsultar o post antes de gravar e abortar se o status deixar de ser `pending`.
   - Testar com servidor HTTP fake e, depois, contra Devilbox.

4. **Snapshot, lock por post e rollback verificável**
   - Criar `src/unicornio_editor/backup.py` e `src/unicornio_editor/locking.py`.
   - Salvar JSON atômico contendo conteúdo bruto, metas, `featured_media`, status e timestamp; aplicar TTL ao lock.
   - Testar concorrência simulada, lock expirado, falha de escrita e restauração do snapshot.

### P1 — Pipeline determinístico e contrato editorial

5. **Sanitização e normalização do HTML**
   - Criar `src/unicornio_editor/html_cleaner.py`.
   - Remover wrappers `article`/`div` inadequados, imagens inline legadas, CTA/fonte anteriores e estruturas inválidas sem alterar fatos textuais.
   - Testar HTML malformado, atributos perigosos, imagens dentro de parágrafos, múltiplos CTAs/fontes e conteúdo vazio.

6. **Schema estrito do JSON editorial**
   - Criar `src/unicornio_editor/editorial_schema.py` e `prompts.py`.
   - Validar `site_relevance`, `cleaned_html`, SEO, `media_plan`, trailer e `original_link`; rejeitar campos desconhecidos/valores fora dos limites.
   - Implementar decisão `skip` quando irrelevante ou abaixo da confiança configurada, sem qualquer chamada de update.

7. **Builder de CTA e fonte canônicos**
   - Criar `src/unicornio_editor/builder.py`.
   - Gerar CTA e fonte exclusivamente a partir de `original_link`, com `target="_blank"` e `rel="nofollow noopener"`; evitar duplicação em reexecuções.
   - Testar URL ausente/malformada, HTML escapado, fonte duplicada e idempotência.

8. **Comandos `list-pending`, `prepare` e `apply`**
   - Criar `src/unicornio_editor/cli.py` e serviços correspondentes.
   - `prepare` deve criar snapshot e relatório sem update; `apply` deve validar tudo, honrar dry-run, reconsultar status e atualizar somente conteúdo/metas/featured media.
   - Emitir JSON de resultado com `wordpress_changed`, `skip_reason`, backup e erros não sensíveis.

### P1 — Integrações WordPress locais

9. **Mapeamento Rank Math**
   - Criar adaptador em `src/unicornio_editor/seo/rank_math.py` para gravar title, description e focus keyword nos metas reais (`rank_math_title`, `rank_math_description`, `rank_math_focus_keyword`), confirmando a instalação local.
   - Testar leitura/escrita REST e preservar metas não gerenciadas.

10. **Fixtures e cenários no Devilbox**
    - Criar documentação/scripts em `tests/wordpress/` para preparar posts locais `pending` relevantes, irrelevantes, com HTML legado, sem `original_link`, com mídia e com mudança concorrente de status.
    - Subir os serviços necessários do Devilbox e registrar o resultado de cada cenário; não usar posts de produção.

### P2 — Mídia pública/licenciada

11. **Descoberta no Google Images e validação de licença**
    - Usar Google Images somente para descoberta; abrir a página original e confirmar domínio público, Creative Commons compatível ou permissão explícita.
    - Exigir no `media_plan`: `source_page_url`, `direct_image_url`, `author`, `license`, `license_url`, `captured_at` e `credit_text`.
    - Recusar qualquer candidato sem evidência. Crédito é obrigatório, mas não substitui autorização.

12. **Download seguro, conversão WebP e upload local**
    - Criar `src/unicornio_editor/media/downloader.py`, `converter.py` e `wordpress_media.py`.
    - Validar MIME/extensão/tamanho/resolução, limitar redirects e bytes, converter para WebP em diretório temporário e limpar arquivos.
    - Enviar pela Media Library REST local, sem bucket, CDN externo ou hotlink.

13. **Inserção de mídia orientada por `media_plan`**
    - Criar `src/unicornio_editor/media/inserter.py`.
    - Inserir somente entre blocos/parágrafos, respeitar máximo padrão de quatro imagens, distância mínima entre imagens, alt text e crédito visível.

14. **Trailer oficial**
    - Criar adaptador separado com allowlist de fontes oficiais, validação de URL e política de falha sem interromper edição textual.
    - Testar `needs_trailer=false`, fonte não oficial e URL inválida.

### P2 — Operação e manutenção

15. **Skill e cron Hermes reproduzíveis**
    - Criar `hermes/SKILL.md` e `hermes/cron-install.sh` conforme as regras existentes, com `--workdir`, batch pequeno, timeout e saída JSON.
    - Testar instalação em sessão isolada e garantir que o cron nunca tenha instruções de publicar.

16. **Observabilidade e marcadores de processamento**
    - Registrar resultados sem conteúdo sensível, com correlation id, post id, decisão, duração e erro categorizado.
    - Definir/adicionar `_ai_editor_*` somente após confirmar registro/exposição REST e política de retenção.

17. **Jobs de manutenção em modo report**
    - Criar comandos separados para imagens quebradas, links quebrados, CTA/fonte ausentes, SEO fraco, WebP ausente, featured image quebrada, posts sem mídia suficiente e mídia órfã.
    - Nenhum job deve alterar WordPress na primeira versão.

18. **CI, documentação e controle de alterações**
    - Inicializar Git, adicionar CI para testes/lint/segredos, remover duplicação entre `README.md` e `IDEA.md`, atualizar `OPERATIONS.md` com comandos reais e documentar rollback.
    - Verificar que `.env`, backups, imagens temporárias, tokens e dumps nunca entram no versionamento.

## Ordem recomendada de execução

`1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13 → 14 → 15 → 16 → 17 → 18`

O primeiro milestone concluível é o pipeline textual em dry-run (`1–10`). Mídia, trailer e upload local devem ficar bloqueados até que o contrato REST, snapshots, validação de licença e cenários locais estejam comprovados.

## Validação global antes de ativar escrita

- Suite unitária e de integração verde.
- Cenários locais positivos, negativos e de concorrência reproduzidos no Devilbox.
- Dry-run gera backup/relatório e `wordpress_changed=false`.
- Caso irrelevante/incerto não altera post.
- Caso relevante atualiza apenas quando a validação passa e o status reconsultado continua `pending`.
- Nenhum caminho aceita ou emite `publish`.
- Segredos ausentes dos logs, backups versionados e artefatos Git.
- Ativação de `EDITOR_DRY_RUN=false` somente após revisão de múltiplos dry-runs.

## Riscos e decisões em aberto

- O plugin SEO confirmado localmente é Rank Math; a versão/configuração ativa e quais campos são expostos pela REST ainda precisam ser verificadas.
- Google Images é somente índice de descoberta; a licença deve ser confirmada na página original.
- Não haverá bucket/CDN externo; a mídia será armazenada localmente na Media Library do WordPress.
- Ainda não há definição de provedor de LLM ou da política operacional para fontes públicas/Creative Commons; isso deve ser fechado antes da automação de mídia.
- É necessário decidir se os metadados `_ai_editor_*` serão expostos via código existente do WordPress ou por configuração/integração já disponível, evitando criar plugin auxiliar sem necessidade.
- O repositório Git foi inicializado e possui política de exclusão de segredos; mudanças futuras devem preservar essa proteção.
