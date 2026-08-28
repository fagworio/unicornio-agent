#!/usr/bin/env python3
"""Monta editorial_107964.json: media_plan com fontes ScreenRant (srcdn byte-stable &amp;)
+ intro com 3 paragrafos (shift +1 nos indices p/ espacamento >=3 incluindo featured)."""
import json, re, sys

SRC = 'backups/107964/editorial.draft.json'
OUT = 'hermes/editorials/editorial_107964.json'

d = json.load(open(SRC))

# --- novo paragrafo de intro (escrito para o shift de indices) ---
new_p = ("<p>O curioso é que o tempo não resolveu nenhuma dessas disputas. Com os catálogos de "
         "streaming mantendo tudo à distância de um clique, cada nova leva de espectadores chega "
         "ao último episódio com expectativas diferentes — e sai dele com opiniões tão firmes quanto "
         "as de quem assistiu ao vivo décadas atrás. De dramas premiados a sitcoms de sucesso, os dez "
         "casos a seguir atravessam gerações e continuam rendendo discussão.</p>")

html = d['cleaned_html']
assert html.count('<h2>') == 10, 'h2 count'
# insere antes do primeiro <h2>
h2pos = html.find('<h2>')
assert h2pos > 0
html = html[:h2pos] + new_p + '\n\n' + html[h2pos:]

# valida: 36 <p>, primeiro <p> de cada secao nos indices esperados
ps = re.findall(r'<p>', html)
assert len(ps) == 36, f'expected 36 p, got {len(ps)}'

# --- media plan ---
SR = 'https://screenrant.com/'
def item(pidx, slug_page, fname, author, obra, credit_obra, alt, q, featured=False):
    return {
        "paragraph_index": pidx,
        "source_page_url": SR + slug_page,
        "direct_image_url": fname,
        "author": author,
        "license": "Uso com crédito",
        "license_url": SR + slug_page,
        "captured_at": "2026-08-28",
        "credit_text": f"Crédito da imagem: {author} (via ScreenRant). {credit_obra}. Uso com crédito.",
        "alt_text": alt,
        "search_query": q,
        "is_featured": featured,
    }

plan = [
    # featured: Supernatural key art (Sam & Dean no final) — subject do seo.title
    item(0, "supernatural-season-15-series-finale-ending-explained-winchesters/",
         "https://static0.srcdn.com/wordpress/wp-content/uploads/2020/11/Jared-Padalecki-as-Sam-Winchester-and-Jensen-Ackles-as-Dean-in-Supernatural-1.jpg?&amp;fit=crop&amp;w=1200&amp;h=675",
         "The CW", "Supernatural", "Supernatural, série com final polêmico",
         "Sam e Dean Winchester em cena do final de Supernatural (Supernatural)",
         "site:screenrant.com supernatural series finale ending explained", True),
    # 1. Game of Thrones
    item(3, "game-thrones-hbo-season-8-bad-reason/",
         "https://static0.srcdn.com/wordpress/wp-content/uploads/2019/05/Game-of-Thrones-Finale-Daenerys-1.jpg?q=49&amp;fit=contain&amp;w=750&amp;h=422&amp;dpr=2",
         "HBO", "Game of Thrones", "Game of Thrones, série com final polêmico",
         "Daenerys Targaryen em Porto Real no final de Game of Thrones (Game of Thrones)",
         "site:screenrant.com game of thrones ending why it failed"),
    # 2. Stranger Things
    item(6, "stranger-things-series-finale-ending-explained/",
         "https://static0.srcdn.com/wordpress/wp-content/uploads/2026/01/eleven-alive-in-stranger-things-series-finale.jpg?q=49&amp;fit=crop&amp;w=825&amp;dpr=2",
         "Netflix", "Stranger Things", "Stranger Things, série com final polêmico",
         "Eleven no final de Stranger Things (Stranger Things)",
         "site:screenrant.com stranger things season 5 ending"),
    # 3. Lost
    item(10, "lost-cast-opinion-series-finale/",
         "https://static0.srcdn.com/wordpress/wp-content/uploads/2025/05/lost-still-from-the-end.jpg?&amp;fit=crop&amp;w=1600&amp;h=900",
         "ABC", "Lost", "Lost, série com final polêmico",
         "Cena do episódio final de Lost (Lost)",
         "site:screenrant.com lost series finale ending explained"),
    # 4. The Sopranos
    item(13, "sopranos-finale-ending-explained-cut-black/",
         "https://static0.srcdn.com/wordpress/wp-content/uploads/2019/07/The-Sopranos-Tony-Soprano-Ending.jpg?q=50&amp;fit=crop&amp;w=825&amp;dpr=1.5",
         "HBO", "The Sopranos", "The Sopranos, série com final polêmico",
         "Tony Soprano no final de The Sopranos (The Sopranos)",
         "site:screenrant.com sopranos series finale ending explained"),
    # 5. How I Met Your Mother
    item(16, "tv-show-finales-most-divisive-controversial/",
         "https://static0.srcdn.com/wordpress/wp-content/uploads/2023/11/himym-funeral.jpg?q=70&amp;fit=contain&amp;w=750&amp;h=422&amp;dpr=1",
         "CBS", "How I Met Your Mother", "How I Met Your Mother, série com final polêmico",
         "Ted lendo para Tracy no final de How I Met Your Mother (How I Met Your Mother)",
         "site:screenrant.com how i met your mother ending finale"),
    # 6. The Boys
    item(19, "the-boys-series-finale-ending-explained/",
         "https://static0.srcdn.com/wordpress/wp-content/uploads/2026/05/butcher-looking-dejected-in-the-boys-season-5-episode-7.jpg?q=49&amp;fit=crop&amp;w=825&amp;dpr=2",
         "Amazon MGM Studios", "The Boys", "The Boys, série com final polêmico",
         "Billy Butcher no final de The Boys (The Boys)",
         "site:screenrant.com the boys season 5 finale ending explained"),
    # 7. Squid Game
    item(22, "squid-game-season-3-episode-6-ending-explained/",
         "https://static0.srcdn.com/wordpress/wp-content/uploads/2025/06/squid-game-season-3-ending-explained-tbd.jpg?&amp;fit=crop&amp;w=1600&amp;h=900",
         "Netflix", "Squid Game", "Squid Game, série com final polêmico",
         "Gi-hun na 3ª temporada de Squid Game (Squid Game)",
         "site:screenrant.com squid game season 3 finale ending explained"),
    # 8. St. Elsewhere
    item(26, "st-elsewhere-controversial-series-finale-revisited/",
         "https://static0.srcdn.com/wordpress/wp-content/uploads/2025/05/st-elsewhere-finale.jpg?q=70&amp;fit=contain&amp;w=750&amp;h=422&amp;dpr=1",
         "NBC", "St. Elsewhere", "St. Elsewhere, série com final polêmico",
         "Tommy Westphall com o globo de neve no final de St. Elsewhere (St. Elsewhere)",
         "site:screenrant.com st elsewhere finale snow globe ending"),
    # 9. Supernatural (inline — imagem distinta da featured)
    item(29, "supernatural-finale-winchesters-ending-controvery-explained/",
         "https://static0.srcdn.com/wordpress/wp-content/uploads/2024/07/supernatural-finale-jared-padalecki-as-sam-winchester-and-jensen-ackles-as-dean-winchester-jpg.jpg?q=49&amp;fit=contain&amp;w=750&amp;h=422&amp;dpr=2",
         "The CW", "Supernatural", "Supernatural, série com final polêmico",
         "Sam e Dean no desfecho de Supernatural (Supernatural)",
         "site:screenrant.com supernatural series finale ending explained"),
    # 10. Twin Peaks
    item(32, "twin-peaks-return-ending-explained/",
         "https://static0.srcdn.com/wordpress/wp-content/uploads/2017/09/Kyle-MacLachlan-and-Sheryl-Lee-in-Twin-Peaks-.jpg?q=50&amp;fit=crop&amp;w=825&amp;dpr=1.5",
         "ABC", "Twin Peaks", "Twin Peaks, série com final polêmico",
         "Dale Cooper e Laura Palmer em Twin Peaks (Twin Peaks)",
         "site:screenrant.com twin peaks ending explained"),
]

# confere espacamento >= 3 entre indices
idxs = [i["paragraph_index"] for i in plan]
for a, b in zip(idxs, idxs[1:]):
    assert b - a >= 3, f'spacing violation {a}->{b}'
assert idxs[-1] <= 35, 'ultimo indice fora do range'

d['media_plan'] = plan
d['cleaned_html'] = html

import os
os.makedirs('hermes/editorials', exist_ok=True)
json.dump(d, open(OUT, 'w'), ensure_ascii=False, indent=2)
print('OK:', OUT)
print('media items:', len(plan), '| indices:', idxs)
print('palavras (aprox):', len(re.sub(r'<[^>]+>', ' ', html).split()))
