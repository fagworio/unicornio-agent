# Análise: comparação de imagens por hash perceptual (pHash)

Status: **IMPLEMENTADO** (gate `imagens_similares` + comando `media-similar`).
Este documento descreve o problema, o método e como usar.

## 1. O problema real medido em produção

Fontes diferentes publicam o **MESMO frame** (screenshot, key art) com **URL,
nome de arquivo e bytes diferentes** (recompressão WebP de cada veículo). Para o
leitor é a mesma imagem; para o código parecem 3 imagens distintas.

Medição (14/09, 20 posts recentes): **7 posts (35%)** com frames repetidos.
Post 113634: 5 imagens, sendo **3 com distância pHash = 0** (idênticas) e a
featured com distância 2 de outra inline.

Agravante: o mínimo rígido 2/4/6 empurra o pipeline a **repetir o mesmo frame**
para atingir a cota quando a obra tem só 1-2 frames distintos (caso 113648: 4
cópias do mesmo key art).

## 2. Por que URL/MD5 não resolvem

- **MD5/SHA**: byte-exato. Uma recompressão WebP, 1 pixel ou metadado diferente
  muda o hash inteiro — o mesmo frame de 3 fontes tem 3 MD5s distintos.
- **Fingerprint de URL** (remover `-1024x576`): pega a mesma imagem em tamanhos
  diferentes do MESMO upload, mas NÃO pega o mesmo frame vindo de uploads/fontes
  diferentes (nomes diferentes).

## 3. O método: hash perceptual (pHash)

`pHash` reduz a imagem a 32x32 em tons de cinza, aplica DCT e mantém as 8x8
frequências baixas → **64 bits**. É robusto a recompressão, escala e pequenas
variações de brilho/contraste.

Comparação = **distância de Hamming** (XOR bit a bit, conta os bits 1):

| Distância | Significado |
|---|---|
| 0 | imagens visualmente idênticas |
| 1-6 | **mesmo frame** (recompressão, crop leve, watermark pequeno) |
| 10-20 | mesma obra/cena parecida, imagem diferente |
| >25 | imagens diferentes |

Limiar adotado: **6** (`DEFAULT_THRESHOLD`) — conservador, pega o mesmo frame
sem bloquear imagens distintas da mesma obra (medidas em 20-34).

## 4. Onde está implementado

**Módulo** `src/unicornio_editor/media/visual_hash.py`:
- `_phash(url)` — baixa (cap 8 MB, UA de navegador, timeout 20 s) e calcula o pHash
- `similar_image_pairs(urls, threshold=6)` — retorna `[(url_a, url_b, dist)]` dos
  pares com distância ≤ limiar
- **fail-soft**: download/formato que falha → imagem ignorada (nunca trava o pipeline)

**Gate pré-publicação** (`checklist.py`): checagem `imagens_similares` roda com as
URLs finais do conteúdo e **bloqueia** o apply se houver qualquer par ≤ 6.

**Comando para o agente** (verificar ANTES de montar o post):
```bash
unicornio-editor media-similar <url1> <url2> ... [--threshold 6]
```
Saída: `{"imagens_comparadas": N, "pares_mesmo_frame": [{a,b,distancia}], "veredito": "REPETIDAS" | "todas distintas"}`

**Dependências**: `Pillow` + `ImageHash` (em `pyproject.toml`).

## 5. Fluxo recomendado (como usar)

1. Ao selecionar candidatos (`media-search-web` / `media-search`), passar as URLs
   escolhidas pelo `media-similar` **antes** de escrever o HTML do post.
2. Manter **1 imagem de cada grupo** (mesmo frame); descartar as demais.
3. Se faltar imagem para o mínimo, buscar mais candidatos e repetir a checagem.
4. O gate no apply é a rede de segurança (bloqueia se alguma escapou).

## 6. Ajuste de limiar

| threshold | Efeito |
|---|---|
| 2-4 | mais estrito (só quase-idênticas) — use se houver falsos negativos |
| **6 (default)** | conservador: pega o mesmo frame re-comprimido/crop leve |
| 8-10 | pega cenas muito parecidas (ângulos próximos) — risco de falso positivo |

Validação real: 3 frames idênticos do 113634 → distância 0 (detectado);
imagens distintas da mesma obra → 20-34 (não bloqueia).

## 7. Limitações honestas

- **Crop severo / mudança de aspecto**: o pHash pode subir (8-14) e não pegar →
  extensão: dHash ou hash resistente a crop.
- **Watermark grande** pode alterar o hash (pode gerar falso negativo).
- **Custo**: 1 download por imagem (no gate e no comando); sem cache persistente.
- Imagens < 32 px ou formato exótico: ignoradas (fail-soft) — nunca travam.
- Não sinaliza "mesma obra, cena diferente" — o que é desejável (são distintas).

## 8. Extensões possíveis

- Persistir o pHash na Media Library (evita re-download e permite comparar contra
  todo o acervo da obra).
- Incluir a **featured** na comparação (hoje a checagem cobre o corpo; a featured
  repetida no corpo foi corrigida nos 11 posts por script separado).
- **Auto-substituição**: ao bloquear, buscar o próximo candidato distinto e
  substituir sem intervenção humana.

## 9. Dimensionamento do mínimo (opção A — implementada)

O pHash resolve dois problemas ligados:

1. **Bloqueio das repetidas** (`imagens_similares`): o apply recusa enquanto
   houver par com distância ≤ 6.
2. **Mínimo proporcional à realidade** (`imagens_no_corpo`): o mínimo exigido
   passa a ser `min(política 2/4/6, frames distintos disponíveis)`. Assim uma
   obra com 3 frames reais publica com 3 imagens distintas em vez de repetir a
   mesma 6x — elimina o incentivo a repetir para atingir a cota.

Implementação (`checklist.py`): os hashes são calculados **uma vez** por apply
(`image_hashes`), a contagem de grupos distintos vem de `distinct_image_count`
(union-find) e os dois checks reusam o mesmo mapa — custo de download único.

Exemplo real (post 113634): 5 imagens → 3 pares repetidos → **3 frames
distintos** → mínimo efetivo `min(6, 3) = 3`; as duas cópias extras são
removidas antes de publicar.
