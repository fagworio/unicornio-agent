# fixtures/ — payloads reais capturados do `state.db`

Insumo do Experimento A (`payload-tools.md`). O que está aqui é o **stdout real que o
agente viu**, extraído das mensagens `role='tool'` de uma sessão do cron já encerrada.

## Regra de proveniência (não negociável)

Toda fixture começa com um cabeçalho com: origem (`state.db`, somente leitura), sessão,
id da mensagem, papel/tool, timestamp UTC, bytes do payload, sha256 e nº de redações.
`index.json` lista as mesmas informações em formato máquina.

Sem cabeçalho e sem sha256, o arquivo não serve como evidência — vira texto solto, e a
comparação de bytes deixa de ser auditável.

## Conteúdo atual

| campo | valor |
|---|---|
| sessão | `cron_9e39343dc6f5_20260922_064523` |
| entregas | 107 (de 164; as < 500 B foram descartadas como ruído) |
| bytes | 318.447 |
| tools presentes | `terminal` 100, `patch` 3, `web_search` 2, `skill_view` 1, `read_file` 1 |
| menor entrega mantida | 500 B |

Como recapturar (ou capturar outra sessão):

```bash
python3 tools/capture_payloads.py --session cron_9e39343dc6f5_20260922_064523 --min-bytes 500
python3 tools/capture_payloads.py --session <outra-sessao> --min-bytes 500 --tool terminal
```

## Redação de credenciais (duas camadas)

1. **Camada do Hermes**: valores com aparência de segredo no stdout de ferramentas já
   saem mascarados como `***` antes de chegar ao modelo.
2. **Camada do capture**: qualquer linha no formato `NOME_DE_CREDENCIAL=` recebe
   `<REDACTADO>` no lugar do valor, e o número de redações vai no cabeçalho e no índice.

Estado atual: 1 redação, em `86020_terminal.txt`, na linha `EDITOR_VISION_API_KEY=***`
(mascaramento da camada 1, re-redigido pela camada 2). **Nenhum valor real de credencial
está presente nos fixtures** — verificado comparando o conteúdo com o `.env` de produção:
o valor real não aparece em nenhum arquivo. A varredura pelo mesmo padrão do
`scripts/check_repository.py` do repo também passa limpa.

Consequência prática: as fixtures podem ser versionadas sem vazar segredo — e é por isso
que o capture não é opcional, é o que torna a captura segura.

## O que estas fixtures NÃO são

- **não são baseline oficial** (são de sessão pré-congelamento, sem `run_source`);
- **não são amostra** (não entram em nenhum denominador de KPI);
- **não substituem a telemetria**: aqui está o TEXTO; o tamanho medido oficialmente está
  em `work/telemetry.jsonl` (evento `cmd_output.bytes`). As duas fontes são cruzadas, não
  uma no lugar da outra.
