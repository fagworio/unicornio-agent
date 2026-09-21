"""Registry de fontes oficiais (SourceResolver, estratégia C do documento).

Para entidades conhecidas (publishers de games, estúdios, portais de anime/
cinema) existe um conjunto pequeno e estável de domínios oficiais. Uma imagem
servida por um desses domínios tem proveniência muito mais forte do que uma
imagem achada num agregador qualquer.

Uso atual (determinístico, sem scraping novo):

* marca o candidato com ``official_source`` (evidência auditável);
* prioriza candidatos oficiais na ordenação final;
* serve de base para a etapa seguinte do resolver: buscar a página dentro
  desses domínios quando o candidato veio de discovery_only.

O registry NÃO substitui o gate de proveniência: uma imagem oficial só entra se
a página de origem confirmar a presença dela (Gate A).
"""

from __future__ import annotations

from urllib.parse import urlparse

# entidade/termo -> domínios oficiais
_DOMINIOS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("nintendo", "mario", "zelda", "metroid", "pokemon"), ("nintendo.com", "nintendo.co.jp", "pokemon.com")),
    (("playstation", "sony", "god of war", "spider-man"), ("playstation.com", "blog.playstation.com", "sony.com")),
    (("xbox", "microsoft", "halo", "forza"), ("xbox.com", "news.xbox.com", "microsoft.com")),
    (("steam", "valve", "counter-strike", "dota"), ("store.steampowered.com", "valvesoftware.com")),
    (("capcom", "resident evil", "street fighter", "monster hunter"), ("capcom.com",)),
    (("square enix", "final fantasy", "kingdom hearts", "dragon quest"), ("square-enix.com", "jp.square-enix.com")),
    (("bandai namco", "tekken", "elden ring", "dark souls", "fromsoftware"), ("bandainamcoent.com", "fromsoftware.jp")),
    (("sega", "sonic", "persona", "atlus"), ("sega.com", "atlus.com")),
    (("ubisoft", "assassin's creed", "far cry"), ("ubisoft.com",)),
    (("electronic arts", "ea sports", "fifa", "battlefield"), ("ea.com",)),
    (("activision", "call of duty"), ("activision.com", "callofduty.com")),
    (("blizzard", "overwatch", "world of warcraft", "diablo"), ("blizzard.com",)),
    (("epic games", "fortnite"), ("epicgames.com", "fortnite.com")),
    (("rockstar", "gta", "red dead"), ("rockstargames.com",)),
    (("cd projekt", "cyberpunk", "the witcher"), ("cdprojektred.com", "thewitcher.com")),
    (("riot", "league of legends", "valorant"), ("riotgames.com",)),
    (("marvel", "mcu", "avengers", "x-men"), ("marvel.com",)),
    (("dc", "batman", "superman", "justice league"), ("dc.com", "dccomics.com")),
    (("disney", "pixar", "star wars", "marvel studios"), ("disney.com", "starwars.com", "pixar.com")),
    (("warner", "hbo", "max", "dc studios"), ("warnerbros.com", "hbo.com", "max.com")),
    (("netflix",), ("netflix.com", "about.netflix.com")),
    (("amazon", "prime video"), ("amazon.com", "primevideo.com")),
    (("apple", "apple tv"), ("apple.com", "tv.apple.com")),
    (("crunchyroll", "anime"), ("crunchyroll.com",)),
    (("toei", "dragon ball", "one piece"), ("toei-anim.co.jp",)),
    (("funimation",), ("funimation.com",)),
    (("bandai", "gundam"), ("bandai.com", "bandainamcoent.com")),
    (("shueisha", "jujutsu kaisen", "naruto", "bleach"), ("shueisha.co.jp",)),
    (("kodansha", "attack on titan"), ("kodansha.co.jp",)),
)


def dominios_oficiais(subject: str, *, extra: str = "") -> list[str]:
    """Domínios oficiais relacionados ao subject (registry determinístico)."""
    alvo = f"{subject or ''} {extra or ''}".lower()
    achados: list[str] = []
    for termos, dominios in _DOMINIOS:
        if any(t in alvo for t in termos):
            for d in dominios:
                if d not in achados:
                    achados.append(d)
    return achados


def official_source(image_url: str, subject: str, *, extra: str = "") -> bool:
    """A imagem é servida por um domínio oficial da entidade do subject?"""
    try:
        host = (urlparse(str(image_url or "")).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in dominios_oficiais(subject, extra=extra))


__all__ = ["dominios_oficiais", "official_source"]
