#!/usr/bin/env python3
"""Recalcula paragraph_index do media_plan: cada imagem inline vai no 1o
paragrafo da secao (indice do H2 numerado + 1). Featured permanece em 0."""
import json, re

def blocks(html):
    pat = re.compile(r'<(h2|h3|p|ul|ol|figure|blockquote|hr)\b[^>]*>.*?</\1>|<hr\s*/?>', re.S)
    return [(m.group(1) or 'hr', m.group(0)) for m in pat.finditer(html)]

for pid in [101845, 101880]:
    ed = json.load(open(f'work/editorial-{pid}.json'))
    blks = blocks(ed['cleaned_html'])
    h2_idx = [i for i, (t, _) in enumerate(blks) if t == 'h2']
    new_plan = []
    for e in ed['media_plan']:
        e = dict(e)
        if e.get('is_featured'):
            e['paragraph_index'] = 0
        else:
            # se ja aponta para o 1o paragrafo apos o H2 da secao, mantem
            target = e['paragraph_index']
            if target in h2_idx:
                target = target + 1
            e['paragraph_index'] = target
        new_plan.append(e)
    ed['media_plan'] = new_plan
    json.dump(ed, open(f'work/editorial-{pid}.json', 'w'), ensure_ascii=False, indent=2)
    print(pid, [(e['paragraph_index'], e['alt_text'][:35]) for e in new_plan])
