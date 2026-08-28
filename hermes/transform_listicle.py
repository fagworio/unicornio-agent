#!/usr/bin/env python3
"""Transforma conteudo de listicle: numera H2s (1..N), desmembra H2 combinado
(101845: Ojamajo Doremi + Wedding Peach), remove CTA/Fonte final, injeta
cleaned_html no editorial existente."""
import json, re

def blocks(html):
    pat = re.compile(r'<(h2|h3|p|ul|ol|figure|blockquote|hr)\b[^>]*>.*?</\1>|<hr\s*/?>', re.S)
    out, pos = [], 0
    for m in pat.finditer(html):
        if m.start() > pos:
            out.append(('raw', html[pos:m.start()]))
        out.append((m.group(1) or 'hr', m.group(0)))
        pos = m.end()
    if pos < len(html):
        out.append(('raw', html[pos:]))
    return out

def renum(blks, split_h2_idx=None):
    """Numera H2s. Se split_h2_idx (indice no bloco original do H2 combinado),
    divide em dois H2s."""
    out, n = [], 0
    for i, (tag, txt) in enumerate(blks):
        if tag == 'h2':
            n += 1
            inner = re.sub(r'<[^>]+>', '', txt)
            inner = re.sub(r'^\s*\d+\.\s*', '', inner).strip()
            if split_h2_idx is not None and i == split_h2_idx:
                h2a = f'<h2>{n}. {inner.split(":")[0].replace(" e Wedding Peach", "")}: honestidade emocional</h2>'
                h2b = f'<h2>{n+1}. Wedding Peach: extensão do heroísmo romântico de Sailor Moon</h2>'
                out.append(('h2', h2a))
                out.append(('h2', h2b))
                n += 1
            else:
                out.append(('h2', f'<h2>{n}. {inner}</h2>'))
        else:
            out.append((tag, txt))
    return out

def drop_fonte(blks):
    """Remove blocos finais de CTA/Fonte (p com Fonte/Portal de Noticias)."""
    end = len(blks)
    for i in range(len(blks) - 1, -1, -1):
        tag, txt = blks[i]
        if tag not in ('p',):
            break
        t = re.sub(r'<[^>]+>', '', txt).strip()
        if re.search(r'fonte|portal de not', t, re.I):
            end = i
        else:
            break
    return blks[:end]

def main():
    edits = {
        101845: {'split': None, 'combined': None},
        101880: {'split': None, 'combined': None},
    }
    # 101845: localizar H2 combinado
    d = json.load(open('work/content_101845.json'))
    blks = blocks(d['cleaned_html'])
    for i, (tag, txt) in enumerate(blks):
        if tag == 'h2' and 'Ojamajo Doremi' in txt and 'Wedding Peach' in txt:
            edits[101845]['combined'] = i
    for pid, cfg in edits.items():
        src = json.load(open(f'work/content_{pid}.json'))
        blks = blocks(src['cleaned_html'])
        blks = renum(blks, cfg['combined'])
        blks = drop_fonte(blks)
        new_html = ''.join(txt for _, txt in blks).strip()
        ed = json.load(open(f'work/editorial-{pid}.json'))
        ed['cleaned_html'] = new_html
        json.dump(ed, open(f'work/editorial-{pid}.json', 'w'), ensure_ascii=False, indent=2)
        nh2 = sum(1 for t, _ in blks if t == 'h2')
        print(f'{pid}: h2 count={nh2}, html len={len(new_html)}')

if __name__ == '__main__':
    main()
