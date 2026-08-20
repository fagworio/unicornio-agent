# Cenários WordPress locais

Estas fixtures são exclusivas do Devilbox e sempre criam posts `pending`.

## Preparação

1. Suba os serviços locais necessários:

```bash
cd /home/joaofagner/workfolder/devilbox
docker compose up -d php httpd mysql
```

2. Configure as credenciais somente no ambiente local, sem gravá-las no repositório:

```bash
export WORDPRESS_URL=http://wordpress.dvl.to:8080
export WORDPRESS_APP_USER=...
export WORDPRESS_APP_PASSWORD=...
```

3. Valide as fixtures sem criar posts:

```bash
PYTHONPATH=src .venv/bin/python tests/wordpress/seed_fixtures.py
```

4. Para criar os posts locais, use explicitamente `--apply`:

```bash
PYTHONPATH=src .venv/bin/python tests/wordpress/seed_fixtures.py --apply
```

O script recusa hosts fora da allowlist local, exige credenciais apenas com `--apply` e aborta se a API retornar qualquer status diferente de `pending`.

## Matriz

- `local-relevant`: conteúdo relevante; deve poder seguir para processamento.
- `local-irrelevant`: conteúdo fora da linha editorial; deve gerar `skip` sem update.
- `local-legacy-html`: wrappers, imagem inline e CTA antigo; deve ser limpo.
- `local-missing-source`: sem `original_link`; deve ser rejeitado sem update.
- `local-concurrency`: simula reclassificação durante o processamento; o update deve abortar se o status deixar de ser `pending`.

Após os testes, remova os posts criados pela API do ambiente local. Não use esta rotina contra produção.
