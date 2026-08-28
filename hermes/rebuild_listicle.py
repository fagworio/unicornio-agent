#!/usr/bin/env python3
"""Regenera cleaned_html dos listicles 101845/101880:
- numera H2s (1..N)
- 101845: desmembra o H2 combinado Doremi+Wedding Peach, colocando os
  paragrafos do Doremi entre os dois H2s
- remove blocos de CTA/Fonte do final e whitespace sozinho
- recalcula paragraph_index: inline = 1o paragrafo apos o H2 da secao,
  featured = 0"""
import json, re

def blocks(html):
    pat = re.compile(r'<(h2|h3|p|ul|ol|figure|blockquote|hr)\b[^>]*>.*?</\1>|<hr\s*/?>', re.S)
    out, pos = [], 0
    for m in pat.finditer(html):
        if m.start() > pos:
            seg = html[pos:m.start()]
            if seg.strip():
                out.append(('raw', seg))
        out.append((m.group(1) or 'hr', m.group(0)))
        pos = m.end()
    if pos < len(html):
        seg = html[pos:]
        if seg.strip():
            out.append(('raw', seg))
    return out

def transform(pid):
    src = json.load(open(f'work/content_{pid}.json'))
    blks = blocks(src['cleaned_html'])
    combined_idx = None
    for i, (t, txt) in enumerate(blks):
        if t == 'h2' and 'Ojamajo Doremi' in txt and 'Wedding Peach' in txt:
            combined_idx = i
    # remove CTA/Fonte do fim
    end = len(blks)
    for i in range(len(blks) - 1, -1, -1):
        t, txt = blks[i]
        if t != 'p':
            if t == 'raw':
                continue
            break
        txt_plain = re.sub(r'<[^>]+>', '', txt).strip()
        if re.search(r'fonte|portal de not', txt_plain, re.I):
            end = i
        else:
            break
    blks = blks[:end]
    # renumera H2s; desmembra o combinado (paragrafos seguintes vao para o H2 novo)
    out, n = [], 0
    skip_until = None
    for i, (t, txt) in enumerate(blks):
        if t == 'h2':
            n += 1
            inner = re.sub(r'<[^>]+>', '', txt)
            inner = re.sub(r'^\s*\d+[\.\)]\s*', '', inner).strip()
            if combined_idx is not None and i == combined_idx:
                # H2 combinado -> vira '9. Ojamajo Doremi'; novo H2 '10. Wedding Peach'
                # depois dos paragrafos do Doremi
                out.append(('h2', f'<h2>{n}. Ojamajo Doremi: honestidade emocional</h2>'))
                # consumir paragrafos de Doremi (proximos 2 ps)
                consumed = 0
                j = i + 1
                while j < len(blks) and consumed < 2 and blks[j][0] == 'p':
                    out.append(blks[j])
                    j += 1
                    consumed += 1
                out.append(('h2', f'<h2>{n+1}. Wedding Peach: extensão do heroísmo romântico de Sailor Moon</h2>'))
                skip_until = j
                n += 1
            else:
                out.append(('h2', f'<h2>{n}. {inner}</h2>'))
        else:
            if skip_until is not None and i < skip_until:
                continue
            out.append((t, txt))
    # raw de whitespace no meio -> preserva (inofensivo), mas remove puramente whitespace
    new_html = ''.join(txt for _, txt in out).strip()
    return new_html

def media_indices(pid, html):
    pat = re.compile(r'<(h2|h3|p|ul|ol|figure|blockquote|hr)\b[^>]*>.*?</\1>|<hr\s*/?>', re.S)
    blks = [(m.group(1) or 'hr', m.group(0)) for m in pat.finditer(html)]
    h2_idx = [i for i, (t, _) in enumerate(blks) if t == 'h2']
    return h2_idx

for pid in [101845, 101880]:
    ed = json.load(open(f'work/editorial-{pid}.json'))
    new_html = transform(pid)
    ed['cleaned_html'] = new_html
    h2_idx = media_indices(pid, new_html)
    # corrige paragraph_index: featured=0; inline = H2 da secao + 1
    # mapeia por ordem: cada media inline corresponde ao H2 da sua secao
    inline = [e for e in ed['media_plan'] if not e.get('is_featured')]
    # ordem das secoes = ordem dos H2s; associa cada imagem ao H2 na mesma ordem
    assert len(inline) == len(h2_idx), f'{pid}: {len(inline)} imgs vs {len(h2_idx)} h2s'
    for e, h in zip(inline, h2_idx):
        e['paragraph_index'] = h + 1
    for e in ed['media_plan']:
        if e.get('is_featured'):
            e['paragraph_index'] = 0
    json.dump(ed, open(f'work/editorial-{pid}.json', 'w'), ensure_ascii=False, indent=2)
    print(pid, 'h2s:', len(h2_idx), 'indices:', [(e['paragraph_index'], e['alt_text'][:30]) for e in ed['media_plan']])
