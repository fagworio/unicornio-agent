# V2 — GPT Vision e identidade visual de mídia

## Contrato

O V2 trata relevância e identidade visual como decisões independentes. A
validação de assunto responde se uma imagem representa o tema editorial; a
identidade visual responde se dois arquivos representam a mesma arte/frame.
Uma decisão de relevância nunca é reutilizada como prova de diversidade.

Cada mídia aceita persiste `sha256`, `phash`, `visual_group_id` e
`visual_verification` no estado V2. Estados antigos são materializados a
partir da URL do attachment antes de poderem servir de baseline. Falha nessa
materialização não prova diversidade e bloqueia apenas o novo candidato.

## Decisão

1. SHA-256 igual rejeita deterministamente.
2. pHash quase idêntico rejeita deterministamente.
3. Nos demais pares, GPT Vision recebe os dois arquivos finais no mesmo pedido,
   com decisão `SAME_IMAGE`, `SAME_ART_CROP`, `DIFFERENT`, `UNCERTAIN` ou
   `ERROR`.
4. `UNCERTAIN` escala uma vez de low para high. `ERROR`/inconclusivo nunca é
   interpretado como `DIFFERENT`.

O cache de comparações fica em `work/visual_comparison_cache.json`, indexado
somente por fingerprints e versão do comparador. O cache de relevância continua
em `work/vision_cache.json`.

## Operação

Os comandos são deliberadamente explícitos:

```bash
unicornio-editor v2-audit-visual-media --post-id ID --root .
unicornio-editor v2-reconcile-visual-media --post-id ID --root .
unicornio-editor v2-reconcile-visual-media --post-id ID --root . --apply
```

O primeiro não escreve. O segundo é dry-run por padrão; com `--apply` atualiza
somente `_hermes_work_state` após materializar todos os fingerprints e confirmar
readback. Não faz upload, não altera conteúdo e não publica.

## Policy

`EDITOR_POLICY_VERSION` padrão é 4, pois a identidade visual integra o
contrato de READY. O rollout deve manter o editorial pausado, executar a
auditoria/reconciliação em uma allowlist controlada e só então processar um
post elegível. Hermes continua responsável por deploy e por qualquer operação
em WordPress de produção.
