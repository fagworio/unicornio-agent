# Contribuindo

## Fluxo de desenvolvimento

1. Trabalhe na branch `main` ou em uma branch curta derivada dela.
2. Preserve a sequência do backlog em `.hermes/plans/`.
3. Para código novo, escreva o teste RED antes da implementação.
4. Rode a suíte completa antes do commit:

```bash
python -m unittest discover -s tests -q
python -m compileall -q src tests scripts
python scripts/check_repository.py
 git diff --check
```

5. Faça commits pequenos e descritivos.

## Segurança

- Nunca commit `.env`, Application Passwords, tokens, cookies, backups ou logs.
- Use credenciais somente em variáveis de ambiente locais.
- Comece com `EDITOR_DRY_RUN=true`.
- Não use posts de produção para testes.
- Imagens exigem evidência de licença e crédito visível.
- O Google Images é apenas mecanismo de descoberta, não fonte de autorização.

## Devilbox

Os cenários WordPress locais estão em `tests/wordpress/`. O seed recusa hosts externos e só cria posts quando executado com `--apply`.

## Commits

Um commit deve conter código/testes/documentação relacionados à mesma mudança. Não inclua artefatos gerados, credenciais ou arquivos temporários.
