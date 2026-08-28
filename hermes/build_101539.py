#!/usr/bin/env python3
"""Build editorial for 101539 (no-rewrite + mechanical cleaned_html fixes)."""
import json, re, subprocess, sys

POST_ID = 101539
SRC_PAGE = "https://screenrant.com/best-playstation-plus-rpgs-perfect-score-games/"

out = subprocess.run(
    [".venv/bin/unicornio-editor", "content", str(POST_ID)],
    capture_output=True, text=True, cwd="/www/wwwroot/hermes/unicornio-agent",
)
d = json.loads(out.stdout)
html = d["cleaned_html"]

# --- 1. H2 colons (estrutura_lista requires "N. Name: description") ---
h2_fixes = {
    "10. Sea of Stars": "10. Sea of Stars: RPG por turnos",
    "9. Horizon Zero Dawn Remastered": "9. Horizon Zero Dawn Remastered: RPG de mundo aberto",
    "8. Final Fantasy 7 Remake Intergrade": "8. Final Fantasy 7 Remake Intergrade: JRPG",
    "6. Disco Elysium": "6. Disco Elysium: RPG de escrita e diálogo",
    "5. Hogwarts Legacy": "5. Hogwarts Legacy: RPG de mundo aberto",
    "4. Cyberpunk 2077": "4. Cyberpunk 2077: RPG de mundo aberto em Night City",
    "3. Persona 5 Royal": "3. Persona 5 Royal: RPG social com combate por turnos",
    "2. Bloodborne": "2. Bloodborne: RPG de ação",
}
for old, new in h2_fixes.items():
    assert old in html, f"H2 not found: {old}"
    html = html.replace(old, new)

# --- 2. Em/en dashes -> commas (qualidade_texto forbids them) ---
html = re.sub(r"\s*[—–]\s*", ", ", html)

# --- 3. Remove trailing "Fonte principal" block + trailing <hr> ---
html = re.sub(
    r"(?:\s*<hr\s*/?>\s*)+<p[^>]*>\s*<a\b[^>]*>\s*Fonte\s+principal\s*</a>\s*</p>\s*$",
    "",
    html,
    flags=re.I | re.S,
)
html = re.sub(r"(?:\s*<hr\s*/?>)+$", "", html, flags=re.I).rstrip()

# --- sanity checks ---
assert not re.search(r"[—–]", html), "dashes remain"
h2s = re.findall(r"<h2[^>]*>(.*?)</h2>", html, re.S)
nums = [re.sub(r"<[^>]+>", "", h).strip() for h in h2s]
print("H2s:", len(h2s))
for n in nums:
    ok = bool(re.match(r"^\d{1,3}\s*[.)-]\s+.+:.+$", n))
    print("  ", ok, n)
ps = re.findall(r"<p[^>]*>", html)
print("<p> count:", len(ps))

media_plan = [
    {
        "paragraph_index": 2,
        "source_page_url": SRC_PAGE,
        "direct_image_url": "https://static0.srcdn.com/wordpress/wp-content/uploads/2024/05/sea-of-stars-key-art.jpg?q=49&amp;fit=crop&amp;w=825&amp;dpr=2",
        "author": "Sabotage Studio (via ScreenRant)",
        "license": "Uso com crédito",
        "license_url": SRC_PAGE,
        "captured_at": "2026-08-28",
        "credit_text": "Crédito da imagem: Sabotage Studio (via ScreenRant). Key art de Sea of Stars, RPG por turnos do PlayStation Plus. Uso com crédito.",
        "alt_text": "Key art de Sea of Stars, RPG por turnos do PlayStation Plus",
        "search_query": "Sea of Stars screenrant key art",
        "is_featured": False,
    },
    {
        "paragraph_index": 5,
        "source_page_url": SRC_PAGE,
        "direct_image_url": "https://static0.srcdn.com/wordpress/wp-content/uploads/2024/11/horizon-zero-dawn-remastered-improved-textures-2.jpg?q=49&amp;fit=crop&amp;w=825&amp;dpr=2",
        "author": "Guerrilla Games (via ScreenRant)",
        "license": "Uso com crédito",
        "license_url": SRC_PAGE,
        "captured_at": "2026-08-28",
        "credit_text": "Crédito da imagem: Guerrilla Games (via ScreenRant). Horizon Zero Dawn Remastered, RPG de mundo aberto do PlayStation Plus. Uso com crédito.",
        "alt_text": "Horizon Zero Dawn Remastered, RPG de mundo aberto do PlayStation Plus",
        "search_query": "Horizon Zero Dawn Remastered screenrant key art",
        "is_featured": False,
    },
    {
        "paragraph_index": 8,
        "source_page_url": SRC_PAGE,
        "direct_image_url": "https://static0.srcdn.com/wordpress/wp-content/uploads/2025/11/final-fantasy-7-remake-intergrade.jpg?q=49&amp;fit=crop&amp;w=825&amp;dpr=2",
        "author": "Square Enix (via ScreenRant)",
        "license": "Uso com crédito",
        "license_url": SRC_PAGE,
        "captured_at": "2026-08-28",
        "credit_text": "Crédito da imagem: Square Enix (via ScreenRant). Final Fantasy 7 Remake Intergrade, JRPG do PlayStation Plus. Uso com crédito.",
        "alt_text": "Final Fantasy 7 Remake Intergrade, JRPG do PlayStation Plus",
        "search_query": "Final Fantasy 7 Remake Intergrade screenrant key art",
        "is_featured": False,
    },
    {
        "paragraph_index": 12,
        "source_page_url": SRC_PAGE,
        "direct_image_url": "https://static0.srcdn.com/wordpress/wp-content/uploads/2025/12/the-elder-scrolls-v-3840x2160-14946.jpg?q=49&amp;fit=crop&amp;w=825&amp;dpr=2",
        "author": "Bethesda (via ScreenRant)",
        "license": "Uso com crédito",
        "license_url": SRC_PAGE,
        "captured_at": "2026-08-28",
        "credit_text": "Crédito da imagem: Bethesda (via ScreenRant). The Elder Scrolls V: Skyrim, RPG de fantasia do PlayStation Plus. Uso com crédito.",
        "alt_text": "The Elder Scrolls V: Skyrim, RPG de fantasia do PlayStation Plus",
        "search_query": "Skyrim screenrant key art",
        "is_featured": False,
    },
    {
        "paragraph_index": 16,
        "source_page_url": SRC_PAGE,
        "direct_image_url": "https://static0.srcdn.com/wordpress/wp-content/uploads/2026/04/disco-elysium-screenshot.jpg?q=49&amp;fit=crop&amp;w=825&amp;dpr=2",
        "author": "ZA/UM (via ScreenRant)",
        "license": "Uso com crédito",
        "license_url": SRC_PAGE,
        "captured_at": "2026-08-28",
        "credit_text": "Crédito da imagem: ZA/UM (via ScreenRant). Disco Elysium, RPG de escrita e diálogo do PlayStation Plus. Uso com crédito.",
        "alt_text": "Disco Elysium, RPG de escrita e diálogo do PlayStation Plus",
        "search_query": "Disco Elysium screenrant key art",
        "is_featured": False,
    },
    {
        "paragraph_index": 19,
        "source_page_url": SRC_PAGE,
        "direct_image_url": "https://static0.srcdn.com/wordpress/wp-content/uploads/2026/03/hogwarts-legacy-2-harry-potter-hbo-show-release-date.jpg?q=49&amp;fit=crop&amp;w=825&amp;dpr=2",
        "author": "Avalanche Software (via ScreenRant)",
        "license": "Uso com crédito",
        "license_url": SRC_PAGE,
        "captured_at": "2026-08-28",
        "credit_text": "Crédito da imagem: Avalanche Software (via ScreenRant). Hogwarts Legacy, RPG de mundo aberto do PlayStation Plus. Uso com crédito.",
        "alt_text": "Hogwarts Legacy, RPG de mundo aberto do PlayStation Plus",
        "search_query": "Hogwarts Legacy screenrant key art",
        "is_featured": False,
    },
    {
        "paragraph_index": 24,
        "source_page_url": SRC_PAGE,
        "direct_image_url": "https://static0.srcdn.com/wordpress/wp-content/uploads/2026/04/cyberpunkl.jpg?q=49&amp;fit=crop&amp;w=825&amp;dpr=2",
        "author": "CD Projekt Red (via ScreenRant)",
        "license": "Uso com crédito",
        "license_url": SRC_PAGE,
        "captured_at": "2026-08-28",
        "credit_text": "Crédito da imagem: CD Projekt Red (via ScreenRant). Cyberpunk 2077, RPG de mundo aberto do PlayStation Plus. Uso com crédito.",
        "alt_text": "Cyberpunk 2077, RPG de mundo aberto do PlayStation Plus",
        "search_query": "Cyberpunk 2077 screenrant key art",
        "is_featured": False,
    },
    {
        "paragraph_index": 29,
        "source_page_url": SRC_PAGE,
        "direct_image_url": "https://static0.srcdn.com/wordpress/wp-content/uploads/2025/12/persona-5-royal-cinematic-cutscene.jpg?q=49&amp;fit=crop&amp;w=825&amp;dpr=2",
        "author": "Atlus (via ScreenRant)",
        "license": "Uso com crédito",
        "license_url": SRC_PAGE,
        "captured_at": "2026-08-28",
        "credit_text": "Crédito da imagem: Atlus (via ScreenRant). Persona 5 Royal, RPG social do PlayStation Plus. Uso com crédito.",
        "alt_text": "Persona 5 Royal, RPG social do PlayStation Plus",
        "search_query": "Persona 5 Royal screenrant key art",
        "is_featured": False,
    },
    {
        "paragraph_index": 33,
        "source_page_url": SRC_PAGE,
        "direct_image_url": "https://static0.srcdn.com/wordpress/wp-content/uploads/2026/04/a-creature-sits-on-a-chair-in-bloodborne.jpg?q=49&amp;fit=crop&amp;w=825&amp;dpr=2",
        "author": "FromSoftware (via ScreenRant)",
        "license": "Uso com crédito",
        "license_url": SRC_PAGE,
        "captured_at": "2026-08-28",
        "credit_text": "Crédito da imagem: FromSoftware (via ScreenRant). Bloodborne, RPG de ação do PlayStation Plus. Uso com crédito.",
        "alt_text": "Bloodborne, RPG de ação do PlayStation Plus",
        "search_query": "Bloodborne screenrant key art",
        "is_featured": False,
    },
    {
        "paragraph_index": 37,
        "source_page_url": SRC_PAGE,
        "direct_image_url": "https://static0.srcdn.com/wordpress/wp-content/uploads/2026/01/tw3-1.png?q=70&amp;fit=crop&amp;w=825&amp;dpr=1",
        "author": "CD Projekt Red (via ScreenRant)",
        "license": "Uso com crédito",
        "license_url": SRC_PAGE,
        "captured_at": "2026-08-28",
        "credit_text": "Crédito da imagem: CD Projekt Red (via ScreenRant). The Witcher 3: Wild Hunt, RPG de fantasia do PlayStation Plus. Uso com crédito.",
        "alt_text": "The Witcher 3: Wild Hunt, RPG de fantasia do PlayStation Plus",
        "search_query": "The Witcher 3 screenrant key art",
        "is_featured": False,
    },
]

editorial = {
    "game_name": None,
    "needs_trailer": False,
    "trailer_url": None,
    "seo": {
        "title": "10 RPGs com nota máxima no PlayStation Plus: Sea of Stars a The Witcher 3",
        "meta_description": "De Sea of Stars a The Witcher 3: os 10 RPGs com nota máxima disponíveis no PlayStation Plus, com destaques de Bloodborne, Cyberpunk 2077 e mais.",
        "focus_keyword": "PlayStation Plus",
    },
    "site_relevance": {
        "decision": "process",
        "confidence": 0.96,
        "matched_topics": ["games", "cultura geek"],
        "reason": "Listicle PT-BR com os 10 melhores RPGs com nota máxima no PlayStation Plus, de Sea of Stars a The Witcher 3, cobrindo jogos como Bloodborne, Cyberpunk 2077, Disco Elysium, Hogwarts Legacy e Persona 5 Royal. Conteúdo completo, com H2s numerados e avaliações por jogo. Fonte: ScreenRant (original_link). Relevante para o portal de games/cultura geek.",
    },
    "media_plan": media_plan,
    "cleaned_html": html,
}

path = "/www/wwwroot/hermes/unicornio-agent/hermes/editorials/editorial_101539.json"
with open(path, "w", encoding="utf-8") as f:
    json.dump(editorial, f, ensure_ascii=False, indent=2)
print("written", path, len(json.dumps(editorial)))
