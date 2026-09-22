"""Pre-check de candidatos de imagem SEM tocar no WordPress (read-only).

Por que existe: `media-validate` NAO roda a verificacao de origem byte-a-byte
(o apply roda). Este harness reproduz exatamente a checagem do apply usando o
MESMO downloader do pipeline, antes de montar/aplicar o plano.

Uso (sempre pelo venv do repo, que tem as deps):

  cd /www/wwwroot/hermes/unicornio-agent
  .venv/bin/python <este_arquivo> <candidatos.json>

candidatos.json = [{"url": "<direct_image_url>", "page": "<source_page_url>"}, ...]

Saida por item: OK/FAIL + dimensoes + flat + transparencia + motivo.
So conte como utilizavel: OK, landscape, largura >= 640px.

Regras confirmadas em producao:
- So passa a URL que aparece LITERALMENTE no HTML servido da pagina de origem
  (og:image, <img src>, srcset) E cujos bytes batem. Variante de tamanho
  inventada (trocar 900x.jpg por 1280x720.jpg) FALHA.
- Em WordPress com Jetpack, o <img> do corpo aponta para i0.wp.com/<host>/...
  com ?resize=WxH&ssl=1: use exatamente essa URL (a "crua" wp-content nao esta
  listada e falha).
- page URL com slug contendo a entidade (nome da obra/jogo) e o que faz a
  featured passar no gate `destaque_relevancia`.
- Rode tambem `unicornio-editor media-similar <url1> <url2> ...` nos candidatos
  aprovados: nomes de arquivo diferentes podem ser o MESMO frame (pHash).
"""
import json
import sys
import tempfile
from pathlib import Path

from PIL import Image

from unicornio_editor.media.downloader import download_image
from unicornio_editor.media.source_verify import verify_downloaded_against_source
from unicornio_editor.media.converter import image_is_mostly_flat, image_has_transparency


def main(path):
    items = json.load(open(path))
    for it in items:
        url, page = it["url"], it["page"]
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "img.bin"
            try:
                download_image(url, dest)
            except Exception as exc:  # noqa: BLE001
                print(f"DOWNLOAD_FAIL {url}\n      {type(exc).__name__}: {exc}")
                continue
            ok, reason = verify_downloaded_against_source(
                source_page_url=page, downloaded=dest, direct_image_url=url
            )
            try:
                im = Image.open(dest)
                size = im.size
                flat = image_is_mostly_flat(dest)
                transp = image_has_transparency(dest)
            except Exception as exc:  # noqa: BLE001
                size, flat, transp = f"ERR {exc}", None, None
            print(f"{'OK  ' if ok else 'FAIL'} {size} flat={flat} transp={transp} :: {url}")
            print(f"      page={page}")
            print(f"      {reason}")


if __name__ == "__main__":
    main(sys.argv[1])
