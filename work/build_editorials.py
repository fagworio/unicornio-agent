"""Build strict editorial JSON for the 4 pending posts (dry-run batch)."""
import json, re

def load(pid):
    return json.load(open(f"work/prepared_{pid}.json"))["cleaned_html"]

def strip_tags(h):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)).strip()

# ---------------------------------------------------------------- 109220
h220 = load(109220)
h220 = re.sub(r"<h1>.*?</h1>", "", h220, flags=re.S).strip()
h220 = h220.replace("–", "-")
# natural keyword insertion (The Witcher 3 sai do Game Pass)
h220 = h220.replace(
    "Segundo a informação divulgada pelo serviço, o jogo deixa a biblioteca no dia <strong>31 de agosto de 2026</strong>.",
    "A confirmação oficial é de que <strong>The Witcher 3 sai do Game Pass em 31 de agosto de 2026</strong>, e o aviso chegou acompanhado de um detalhe que costuma gerar curiosidade entre os jogadores.",
)

# ---------------------------------------------------------------- 109222
h222 = load(109222)
# ensure the focus keyword appears naturally in the intro
h222 = h222.replace(
    "A cultura do anime deixou de ser apenas uma experiência de tela.",
    "A cultura do anime deixou de ser apenas uma experiência de tela, e as experiências de anime presenciais viraram um dos segmentos que mais crescem no entretenimento asiático.",
)

# ---------------------------------------------------------------- 109224
h224 = load(109224)
# keyword: "Nintendo Today! 4.1.0" (must appear in body verbatim)
h224 = h224.replace(
    "De acordo com o comunicado oficial, a atualização <strong>4.1.0</strong> foi lançada em <strong>20 de agosto de 2026</strong>.",
    "De acordo com o comunicado oficial, o <strong>Nintendo Today! 4.1.0</strong> foi lançado em <strong>20 de agosto de 2026</strong>.",
)

# ---------------------------------------------------------------- 109226
h226 = load(109226)
h226 = h226.replace("–", "-")
# keyword: "séries como Lioness"
h226 = h226.replace(
    "Se você já terminou os episódios e está esperando o próximo domingo, ou se fez uma maratona e precisa de algo para preencher o intervalo, a boa notícia é que há alternativas.",
    "Se você já terminou os episódios e está esperando o próximo domingo, a boa notícia é que há alternativas: reunimos sete séries como Lioness, com foco em apostas altas, ritmo de maratona e personagens que carregam consequências reais.",
)
# numbered H2s for list validation (1..7, in article order)
items = [
    ("“The Recruit” (2022-2025)", "o thriller de espionagem com energia mais leve"),
    ("“Black Doves” (2024-presente)", "espionagem, vingança e um ritmo que prende"),
    ("“SEAL Team” (2017-2024)", "o drama militar que mostra o custo real"),
    ("“Bodyguard” (2018)", "seis episódios de paranoia política"),
    ("“Tehran” (2020-presente)", "o thriller internacional com operações que desmoronam"),
    ("“Elite Force” (2026)", "o thriller francês de GIGN feito para acelerar"),
    ("“The Night Agent” (2023-2026)", "do FBI comum ao alvo global"),
]
for i, (name, desc) in enumerate(items, start=1):
    old = f"<h2>{name}: {desc}</h2>"
    new = f"<h2>{i}. {name}: {desc}</h2>"
    assert old in h226, f"missing h2 for item {i}"
    h226 = h226.replace(old, new)
h226 = re.sub(r"\s*<hr\s*/?>\s*", "\n\n", h226).strip()

# ---------------------------------------------------------------- media plans
def media(title, page, direct, author, license_name, license_url, date, credit, alt, featured=False, index=0):
    return {
        "paragraph_index": index,
        "source_page_url": page,
        "direct_image_url": direct,
        "author": author,
        "license": license_name,
        "license_url": license_url,
        "captured_at": date,
        "credit_text": credit,
        "alt_text": alt,
        "is_featured": featured,
    }

W = "https://upload.wikimedia.org/wikipedia/commons"
C = "https://commons.wikimedia.org/wiki/"

plan220 = [
    media(
        "The Witcher 3 Wild Hunt - Bloody Baron.jpg", C + "File:The_Witcher_3_Wild_Hunt_-_Bloody_Baron.jpg",
        W + "/d/d1/The_Witcher_3_Wild_Hunt_-_Bloody_Baron.jpg",
        "BANDAI NAMCO Entertainment Europe", "CC BY 3.0", "https://creativecommons.org/licenses/by/3.0", "2015-04-10",
        "Crédito da imagem: BANDAI NAMCO Europe (via Wikimedia Commons). Cena do Barão Sanguinário em The Witcher 3: Wild Hunt, extraída do trailer oficial de gameplay. Licença CC BY 3.0 (https://creativecommons.org/licenses/by/3.0).",
        "Cena do Barão Sanguinário no jogo The Witcher 3: Wild Hunt",
        featured=True, index=1,
    ),
    media(
        "The Witcher 3 Wild Hunt - Morvudd fight.jpg", C + "File:The_Witcher_3_Wild_Hunt_-_Morvudd_fight.jpg",
        W + "/b/bf/The_Witcher_3_Wild_Hunt_-_Morvudd_fight.jpg",
        "BANDAI NAMCO Entertainment Europe", "CC BY 3.0", "https://creativecommons.org/licenses/by/3.0", "2015-04-10",
        "Crédito da imagem: BANDAI NAMCO Europe (via Wikimedia Commons). Geralt de Rívia em combate contra Morvudd em The Witcher 3: Wild Hunt. Licença CC BY 3.0 (https://creativecommons.org/licenses/by/3.0).",
        "Geralt de Rívia em combate em The Witcher 3: Wild Hunt",
        index=3,
    ),
    media(
        "The Witcher 3 Wild Hunt - Dialogue options.jpg", C + "File:The_Witcher_3_Wild_Hunt_-_Dialogue_options.jpg",
        W + "/6/63/The_Witcher_3_Wild_Hunt_-_Dialogue_options.jpg",
        "BANDAI NAMCO Entertainment Europe", "CC BY 3.0", "https://creativecommons.org/licenses/by/3.0", "2015-04-10",
        "Crédito da imagem: BANDAI NAMCO Europe (via Wikimedia Commons). Sistema de diálogo com opções de escolha em The Witcher 3: Wild Hunt. Licença CC BY 3.0 (https://creativecommons.org/licenses/by/3.0).",
        "Opções de diálogo em The Witcher 3: Wild Hunt",
        index=6,
    ),
    media(
        "The Witcher 3 Wild Hunt - Enraged Cirilla.jpg", C + "File:The_Witcher_3_Wild_Hunt_-_Enraged_Cirilla.jpg",
        W + "/d/dc/The_Witcher_3_Wild_Hunt_-_Enraged_Cirilla.jpg",
        "BANDAI NAMCO Entertainment Europe", "CC BY 3.0", "https://creativecommons.org/licenses/by/3.0", "2015-04-10",
        "Crédito da imagem: BANDAI NAMCO Europe (via Wikimedia Commons). Cirilla em cena de The Witcher 3: Wild Hunt, extraída do trailer oficial de gameplay. Licença CC BY 3.0 (https://creativecommons.org/licenses/by/3.0).",
        "Cirilla em cena de The Witcher 3: Wild Hunt",
        index=9,
    ),
]

plan222 = [
    media(
        "Cosplay of Portgas D. Ace and Dracule Mihawk from One Piece at Japan Expo 2019 (48449545521).jpg",
        C + "File:Cosplay_of_Portgas_D._Ace_and_Dracule_Mihawk_from_One_Piece_at_Japan_Expo_2019_%2848449545521%29.jpg",
        W + "/3/39/Cosplay_of_Portgas_D._Ace_and_Dracule_Mihawk_from_One_Piece_at_Japan_Expo_2019_%2848449545521%29.jpg",
        "Miguel Discart", "CC BY-SA 2.0", "https://creativecommons.org/licenses/by-sa/2.0", "2019-07-05",
        "Crédito da imagem: Miguel Discart (via Wikimedia Commons). Cosplay de Portgas D. Ace e Dracule Mihawk, de One Piece, na Japan Expo 2019. Licença CC BY-SA 2.0 (https://creativecommons.org/licenses/by-sa/2.0).",
        "Cosplay de personagens de One Piece em evento de cultura pop",
        featured=True, index=1,
    ),
    media(
        "Cosplay de Sangoku de Dragon Ball (Glénat) à Japan Expo 2014 (14669941346).jpg",
        C + "File:Cosplay_de_Sangoku_de_Dragon_Ball_%28Gl%C3%A9nat%29_%C3%A0_Japan_Expo_2014_%2814669941346%29.jpg",
        W + "/3/39/Cosplay_de_Sangoku_de_Dragon_Ball_%28Gl%C3%A9nat%29_%C3%A0_Japan_Expo_2014_%2814669941346%29.jpg",
        "ActuaLitté", "CC BY-SA 2.0", "https://creativecommons.org/licenses/by-sa/2.0", "2014-07-05",
        "Crédito da imagem: ActuaLitté (via Wikimedia Commons). Cosplay de Goku, de Dragon Ball, na Japan Expo 2014. Licença CC BY-SA 2.0 (https://creativecommons.org/licenses/by-sa/2.0).",
        "Cosplay de Goku de Dragon Ball em convenção",
        index=4,
    ),
    media(
        "Cosplay of Evangelion Unit-01 and cosplay mash-up of Asuka Langley Soryu and Evangelion Unit-02 from Neon Genesis Evangelion at FanimeCon 2023 (53055067012).jpg",
        C + "File:Cosplay_of_Evangelion_Unit-01_and_cosplay_mash-up_of_Asuka_Langley_Soryu_and_Evangelion_Unit-02_from_Neon_Genesis_Evangelion_at_FanimeCon_2023_%2853055067012%29.jpg",
        W + "/2/2e/Cosplay_of_Evangelion_Unit-01_and_cosplay_mash-up_of_Asuka_Langley_Soryu_and_Evangelion_Unit-02_from_Neon_Genesis_Evangelion_at_FanimeCon_2023_%2853055067012%29.jpg",
        "LX-Designs", "CC BY-SA 2.0", "https://creativecommons.org/licenses/by-sa/2.0", "2023-05-28",
        "Crédito da imagem: LX-Designs (via Wikimedia Commons). Cosplay de Evangelion Unit-01 e Asuka de Neon Genesis Evangelion no FanimeCon 2023. Licença CC BY-SA 2.0 (https://creativecommons.org/licenses/by-sa/2.0).",
        "Cosplay de Evangelion em convenção de cultura pop",
        index=8,
    ),
    media(
        "Cosplayers of Dragon Ball Z at AWA14 20080920.jpg",
        C + "File:Cosplayers_of_Dragon_Ball_Z_at_AWA14_20080920.jpg",
        W + "/3/32/Cosplayers_of_Dragon_Ball_Z_at_AWA14_20080920.jpg",
        "mikemol", "CC BY 2.0", "https://creativecommons.org/licenses/by/2.0", "2008-09-20",
        "Crédito da imagem: mikemol (via Wikimedia Commons). Grupo de cosplayers de Dragon Ball Z no Anime Weekend Atlanta 14. Licença CC BY 2.0 (https://creativecommons.org/licenses/by/2.0).",
        "Grupo de cosplayers de Dragon Ball Z em convenção",
        index=11,
    ),
]

plan224 = [
    media(
        "Nintendo-Switch-Console-Docked-wJoyConRB.jpg", C + "File:Nintendo-Switch-Console-Docked-wJoyConRB.jpg",
        W + "/7/76/Nintendo-Switch-Console-Docked-wJoyConRB.jpg",
        "Evan-Amos", "Public domain", "https://commons.wikimedia.org/wiki/Commons:Public_domain", "2017-06-21",
        "Crédito da imagem: Evan-Amos (via Wikimedia Commons). Console Nintendo Switch no modo dock, com controles Joy-Con. Domínio público.",
        "Console Nintendo Switch no modo dock",
        featured=True, index=1,
    ),
    media(
        "HK SW 上環 Sheung Wan 德輔道中 Des Voeux Road Central tram stop n body Pikmin March 2026 N13P 01.jpg",
        C + "File:HK_SW_%E4%B8%8A%E7%92%B0_Sheung_Wan_%E5%BE%B7%E8%BC%94%E9%81%93%E4%B8%AD_Des_Voeux_Road_Central_tram_stop_n_body_Pikmin_March_2026_N13P_01.jpg",
        W + "/d/d8/HK_SW_%E4%B8%8A%E7%92%B0_Sheung_Wan_%E5%BE%B7%E8%BC%94%E9%81%93%E4%B8%AD_Des_Voeux_Road_Central_tram_stop_n_body_Pikmin_March_2026_N13P_01.jpg",
        "Homm BoUretak BAORP", "CC0", "https://creativecommons.org/publicdomain/zero/1.0/", "2026-03-28",
        "Crédito da imagem: Homm BoUretak BAORP (via Wikimedia Commons). Ônibus com propaganda de Pikmin em Hong Kong, março de 2026. Licença CC0 (domínio público).",
        "Propaganda de Pikmin em veículo público em Hong Kong",
        index=3,
    ),
    media(
        "Otakuthon 2014- Olimar and his Pikmin harem (14843007360).jpg",
        C + "File:Otakuthon_2014-_Olimar_and_his_Pikmin_harem_%2814843007360%29.jpg",
        W + "/1/1e/Otakuthon_2014-_Olimar_and_his_Pikmin_harem_%2814843007360%29.jpg",
        "Pikawil", "CC BY 2.0", "https://creativecommons.org/licenses/by/2.0", "2014-08-22",
        "Crédito da imagem: Pikawil (via Wikimedia Commons). Cosplay do Capitão Olimar e Pikmin na Otakuthon 2014. Licença CC BY 2.0 (https://creativecommons.org/licenses/by/2.0).",
        "Cosplay do Capitão Olimar e Pikmin em convenção",
        index=6,
    ),
    media(
        "Nintendo Switch 2 in Handheld Mode.jpg", C + "File:Nintendo_Switch_2_in_Handheld_Mode.jpg",
        W + "/5/50/Nintendo_Switch_2_in_Handheld_Mode.jpg",
        "Crisco 1492", "CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0", "2025-06-06",
        "Crédito da imagem: Crisco 1492 (via Wikimedia Commons). Nintendo Switch 2 no modo portátil. Licença CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0).",
        "Nintendo Switch 2 no modo portátil",
        index=9,
    ),
]

plan226 = [
    media(
        "Zoe Saldana at the 2024 Toronto International Film Festival 4.jpg",
        C + "File:Zoe_Saldana_at_the_2024_Toronto_International_Film_Festival_4.jpg",
        W + "/2/2c/Zoe_Saldana_at_the_2024_Toronto_International_Film_Festival_4.jpg",
        "Frank Sun", "CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0", "2024-09-09",
        "Crédito da imagem: Frank Sun (via Wikimedia Commons). Zoe Saldaña no Festival Internacional de Cinema de Toronto de 2024; a atriz vive Joe em Lioness. Licença CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0).",
        "Zoe Saldaña no Festival de Toronto de 2024",
        featured=True, index=1,
    ),
    media(
        "Actor Noah Centineo Visits Naval Station Norfolk (5).jpg",
        C + "File:Actor_Noah_Centineo_Visits_Naval_Station_Norfolk_%285%29.jpg",
        W + "/6/65/Actor_Noah_Centineo_Visits_Naval_Station_Norfolk_%285%29.jpg",
        "Mass Communication Specialist 2nd Class Ace Foster (U.S. Navy)", "Public domain", "https://commons.wikimedia.org/wiki/Commons:Public_domain", "2025-02-10",
        "Crédito da imagem: U.S. Navy, foto de Ace Foster (via Wikimedia Commons). Noah Centineo, astro de The Recruit, visita a Estação Naval de Norfolk. Domínio público (obra do governo dos EUA).",
        "Noah Centineo, de The Recruit, em visita à Estação Naval de Norfolk",
        index=3,
    ),
    media(
        "Richard Madden (48462874707) (cropped).jpg",
        C + "File:Richard_Madden_%2848462874707%29_%28cropped%29.jpg",
        W + "/3/3b/Richard_Madden_%2848462874707%29_%28cropped%29.jpg",
        "Gage Skidmore", "CC BY-SA 2.0", "https://creativecommons.org/licenses/by-sa/2.0", "2019-07-20",
        "Crédito da imagem: Gage Skidmore (via Wikimedia Commons). Richard Madden, protagonista de Bodyguard, na San Diego Comic-Con 2019. Licença CC BY-SA 2.0 (https://creativecommons.org/licenses/by-sa/2.0).",
        "Richard Madden, protagonista de Bodyguard, na Comic-Con 2019",
        index=10,
    ),
    media(
        "Niv Sultain playing in the Israeli TV series titled Tehran (Apple TV+) - season 2 (cropped).jpg",
        C + "File:Niv_Sultain_playing_in_the_Israeli_TV_series_titled_Tehran_%28Apple_TV%2B%29_-_season_2_%28cropped%29.jpg",
        W + "/d/d2/Niv_Sultain_playing_in_the_Israeli_TV_series_titled_Tehran_%28Apple_TV%2B%29_-_season_2_%28cropped%29.jpg",
        "Domniki Mitropoulou / Kan 11 / Apple TV", "CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0", "2022-05-05",
        "Crédito da imagem: Domniki Mitropoulou / Kan 11 / Apple TV (via Wikimedia Commons). Niv Sultan na série Tehran, do Apple TV+, segunda temporada. Licença CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0).",
        "Niv Sultan na série Tehran, do Apple TV+",
        index=14,
    ),
]

def editorial(pid, html, seo_title, meta_desc, keyword, topics, plan, game_name=None):
    return {
        "site_relevance": {
            "decision": "process",
            "confidence": 0.96,
            "reason": f"Conteudo alinhado com a linha editorial do UnicornioHater (topics: {', '.join(topics)}); fonte original registrada em original_link.",
            "matched_topics": topics,
        },
        "cleaned_html": html,
        "seo": {"title": seo_title, "meta_description": meta_desc, "focus_keyword": keyword},
        "media_plan": plan,
        "needs_trailer": False,
        "trailer_url": None,
        "game_name": game_name,
    }

posts = {
    109220: dict(
        html=h220,
        seo_title="The Witcher 3 sai do Game Pass em 31 de agosto de 2026",
        meta_desc="The Witcher 3 sai do Game Pass em 31 de agosto de 2026. Veja o que isso significa para quem joga a Complete Edition e como se preparar antes da saída.",
        keyword="The Witcher 3 sai do Game Pass",
        topics=["games", "xbox", "game pass"],
        plan=plan220, game_name="The Witcher 3: Wild Hunt",
    ),
    109222: dict(
        html=h222,
        seo_title="INCUBASE Studio leva experiências de anime em circuito pela Ásia",
        meta_desc="Experiências de anime em circuito: INCUBASE Studio leva One Piece, Dragon Ball e Evangelion a Jeju, Singapura e outros destinos, com adaptação local.",
        keyword="experiências de anime",
        topics=["animes", "cultura geek", "eventos"],
        plan=plan222,
    ),
    109224: dict(
        html=h224,
        seo_title="Nintendo Today! 4.1.0: múltiplos temas e Pikmin no Gamescom",
        meta_desc="Nintendo Today! 4.1.0 chega com múltiplos temas aleatórios, correções de bugs e caça a Pikmin escondidos no Gamescom, entre 25 e 30 de agosto.",
        keyword="Nintendo Today! 4.1.0",
        topics=["nintendo", "games", "aplicativos"],
        plan=plan224,
    ),
    109226: dict(
        html=h226,
        seo_title="7 séries como Lioness para maratonar no fim de semana",
        meta_desc="Sete séries como Lioness para maratonar no fim de semana: espionagem, ação e suspense com The Recruit, Black Doves, SEAL Team, Bodyguard, Tehran e mais.",
        keyword="séries como Lioness",
        topics=["series", "streaming", "cultura pop"],
        plan=plan226,
    ),
}

for pid, spec in posts.items():
    html = spec["html"]
    # sanity: keyword verbatim in title and body
    assert spec["keyword"].casefold() in spec["seo_title"].casefold(), f"{pid}: keyword not in title"
    assert spec["keyword"].casefold() in strip_tags(html).casefold(), f"{pid}: keyword not in body"
    assert len(spec["seo_title"]) <= 65, f"{pid}: seo title {len(spec['seo_title'])} chars"
    assert 120 <= len(spec["meta_desc"]) <= 160, f"{pid}: meta desc {len(spec['meta_desc'])} chars"
    assert "—" not in html and "–" not in html, f"{pid}: en/em dash remains"
    # paragraph indices sanity for insert_media (dry-run reports only, but plan must be coherent)
    ends = len(re.findall(r"</p>", html))
    for item in spec["plan"]:
        assert item["paragraph_index"] < ends - 1, f"{pid}: paragraph_index out of range"
    featured = [m for m in spec["plan"] if m["is_featured"]]
    assert len(featured) <= 1, f"{pid}: >1 featured"
    for m in spec["plan"]:
        assert m["credit_text"].startswith("Crédito da imagem:"), f"{pid}: credit format"
        assert m["license_url"].startswith("http"), f"{pid}: license_url"
    out = editorial(pid, html, spec["seo_title"], spec["meta_desc"], spec["keyword"], spec["topics"], spec["plan"], spec.get("game_name"))
    with open(f"work/editorial_{pid}.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print(f"{pid}: OK seo_title={len(spec['seo_title'])} meta={len(spec['meta_desc'])} paras={ends} plan={len(spec['plan'])} game={spec.get('game_name')}")
print("done")
