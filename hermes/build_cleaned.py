import json, re, subprocess

def cli_content(post_id):
    r = subprocess.run(
        ["set -a && . ./.env && set +a && .venv/bin/unicornio-editor content %d" % post_id],
        shell=True, capture_output=True, text=True, cwd="/www/wwwroot/hermes/unicornio-agent", timeout=120)
    d = json.loads(r.stdout)
    return d.get("cleaned_html", "")

h = cli_content(111137)
print("orig </p>:", len(re.findall(r"</p>", h)), "| <li>:", len(re.findall(r"<li", h)))
# locate the track-list <ol>: the one containing 'Fire Emblem: Three Houses Main Theme'
m = re.search(r"<ol>\s*<li>.*?</ol>", h, flags=re.S)
assert m, "ol track list not found"
ol = m.group(0)
print("ol len:", len(ol), "| li in ol:", len(re.findall(r"<li>", ol)))
# transform li items inside the ol
def li_to_p(mm):
    inner = mm.group(1)
    return "<li><p>%s</p></li>" % inner
ol_new = re.sub(r"<li>(.*?)</li>", li_to_p, ol, flags=re.S)
print("ol_new li><p>:", len(re.findall(r"<li><p>", ol_new)))
# split after li52 (0-based) -> 53rd item
starts = [x.start() for x in re.finditer(r"<li><p>", ol_new)]
assert len(starts) == 103, len(starts)
cut_li_end = ol_new.find("</li>", starts[52]) + len("</li>")
split_at = ol_new.find("</ol>")
assert cut_li_end < split_at
ol_final = (ol_new[:cut_li_end] + "</ol>\n<ol start=\"54\">" + ol_new[starts[53]:split_at] + "</ol>")
# sanity: count
print("ol_final <ol>:", ol_final.count("<ol"), ol_final.count("</ol>"), "| li:", len(re.findall(r"<li><p>", ol_final)))
# reassemble
h_new = h[:m.start()] + ol_final + h[m.end():]
print("final </p>:", len(re.findall(r"</p>", h_new)), "| dashes:", len(re.findall(r"[—–]", h_new)))
json.dump(h_new, open("/tmp/cleaned_111137.html.json", "w"), ensure_ascii=False)
print("OK 111137")

# 111135
h135 = cli_content(111135)
n = len(re.findall(r"[—–]", h135))
h135_new = h135.replace("—", "-").replace("–", "-")
print("111135 dashes:", n, "| </p>:", len(re.findall(r"</p>", h135_new)))
json.dump(h135_new, open("/tmp/cleaned_111135.html.json", "w"), ensure_ascii=False)
print("OK 111135")
