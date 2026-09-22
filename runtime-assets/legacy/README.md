# runtime-assets/legacy/ — artefatos implantados SEM origem no Git

Dois scripts que o Hermes tem implantados fora do repositório e cuja única origem
histórica era `/tmp` (transiente). O experimento os registrou por hash; aqui ficam as
**cópias congeladas** para que a próxima versão não dependa de um arquivo de `/tmp`.

Nada aqui é importado pelo runtime, nada aqui substitui produção, e nada aqui deve ser
editado: é evidência, não código ativo.

## Snapshot

| arquivo | sha256 | bytes | mtime do original (UTC) | origem | uso atual |
|---|---|---|---|---|---|
| `commons_search.py` | `1b21922360b08e2e99ea0d4f3c6036390d35265da47b0f3096e24af6f453da96` | 3259 | 2026-09-17T08:00:15Z | **desconhecida** (instalado à mão; não há fonte no Git) | órfão: não citado pelo `SKILL.md`, nem por `references/`, nem por `src/`, `tests/`, `scripts/` |
| `precheck_media.py` | `e276a9eacc419c7a0c1c05536b1dc53386fa379f2ae5ff35d5aa81e665408fe4` | 2766 | 2026-09-16T07:29:57Z | **desconhecida**; mesmo sha256 de `/tmp/precheck_media.py` (arquivo transiente) | órfão: idem acima |

Cópias conferidas byte a byte contra o original implantado (`cmp -s` → idêntico).
Origem no runtime: `~/.hermes/skills/unicorniohater-editor/scripts/`.

## Evidência da órfandade (por que "não citado" importa)

- `hermes/SKILL.md` cita `scripts/diagnostico.sh`, que resolve contra o **workdir do job**
  (`/www/wwwroot/hermes/unicornio-agent/scripts/diagnostico.sh`) — não contra a pasta
  `scripts/` da skill implantada.
- `grep` por `commons_search`/`precheck_media` em `src/`, `tests/`, `hermes/`, `scripts/`
  não encontra referência.
- Existe um candidato divergente em `work/commons_search.py` (`work/` é git-ignored, ou
  seja: também não é fonte versionada) e um `work/precheck.py` diferente do
  `precheck_media.py` implantado.

Consequência: o risco de reprodutibilidade é **latente**, não ativo — mas enquanto não
forem versionados ou removidos, o runtime carrega dois arquivos sem dono dentro da pasta
da skill.

## O que fazer depois do experimento (não antes)

1. Se algum deles ainda tiver função: promover para código versionado (`src/` ou
   `scripts/`), com referência explícita no `SKILL.md` — e então remover a cópia solta da
   skill.
2. Se não tiver função: remover de `~/.hermes/skills/.../scripts/` (é poda de runtime,
   então só depois de encerrar o congelamento).
3. Em qualquer caso: nunca mais depender de `/tmp` como origem de artefato implantado.

Este diretório existe para que a decisão de (1) ou (2) tenha a evidência na mão: hash,
tamanho, data e uso declarado.
