"""Поиск картинок-кандидатов: python3 find_pics.py "<запрос по-английски>" <папка ролика> [--n 8] [--url URL ...]

Ищет в Wikimedia Commons и Openverse (свободные лицензии), качает до N кандидатов в
`<папка ролика>/кандидаты/<запрос>/` и собирает лист с номерами `кандидаты/<запрос>.jpg`.
Claude открывает лист, выбирает номер и берёт файл `NN.jpg` из той же папки. Источник и лицензия каждого
кандидата — в `sources.json` рядом, их надо переписать в «источники картинок.txt».

`--url` — скачать конкретные картинки (например, кадр-мем, найденный веб-поиском) и добавить их в тот же лист.
"""
import argparse
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw

UA = "content-factory/1.0 (https://github.com/atarverum/content-factory)"


def get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def commons(query: str, n: int) -> list[dict]:
    params = {
        "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
        "gsrsearch": f"filetype:bitmap {query}", "gsrlimit": n, "prop": "imageinfo",
        "iiprop": "url|extmetadata", "iiurlwidth": 1000,
    }
    data = json.loads(get("https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params)))
    out = []
    for p in sorted(data.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 0)):
        ii = (p.get("imageinfo") or [{}])[0]
        meta = ii.get("extmetadata", {})
        lic = meta.get("LicenseShortName", {}).get("value", "")
        if re.search(r"\b(nc|nd)\b", lic.lower()):
            continue
        out.append({"img": ii.get("thumburl") or ii.get("url"), "page": ii.get("descriptionurl"),
                    "title": p.get("title", ""), "license": lic,
                    "author": re.sub("<[^>]+>", "", meta.get("Artist", {}).get("value", ""))[:120]})
    return out


def openverse(query: str, n: int) -> list[dict]:
    params = {"q": query, "page_size": n, "license_type": "commercial,modification", "mature": "false"}
    data = json.loads(get("https://api.openverse.org/v1/images/?" + urllib.parse.urlencode(params)))
    return [{"img": r.get("url"), "page": r.get("foreign_landing_url"), "title": r.get("title", ""),
             "license": f'{r.get("license", "")} {r.get("license_version", "")}'.strip(),
             "author": r.get("creator", "")} for r in data.get("results", [])]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("workdir")
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--url", nargs="*", default=[])
    a = ap.parse_args()

    slug = re.sub(r"[^\w-]+", "_", a.query.lower()).strip("_")[:40] or "query"
    folder = os.path.join(os.path.expanduser(a.workdir), "кандидаты", slug)
    os.makedirs(folder, exist_ok=True)

    found = [{"img": u, "page": u, "title": "указанная ссылка", "license": "проверить вручную", "author": ""}
             for u in a.url]
    for name, fn in (("commons", commons), ("openverse", openverse)):
        try:
            found += fn(a.query, a.n)
        except Exception as e:  # сеть или лимит: второй источник всё равно попробуем
            print(f"{name}: не получилось ({e})", file=sys.stderr)

    saved, seen = [], set()
    for c in found:
        if len(saved) >= a.n + len(a.url) or not c.get("img") or c["img"] in seen:
            continue
        seen.add(c["img"])
        try:
            im = Image.open(io.BytesIO(get(c["img"]))).convert("RGB")
        except Exception:
            continue
        if min(im.size) < 300:
            continue
        im.thumbnail((1600, 1600))
        k = len(saved) + 1
        path = os.path.join(folder, f"{k:02d}.jpg")
        im.save(path, quality=90)
        saved.append({**c, "n": k, "file": path, "size": im.size})

    with open(os.path.join(folder, "sources.json"), "w") as f:
        json.dump(saved, f, ensure_ascii=False, indent=1)
    if not saved:
        print("Ничего не нашлось: попробуй другой запрос (по-английски, проще) или --url.")
        return

    tw, th, cols = 300, 300, 4
    rows = (len(saved) + cols - 1) // cols
    sheet = Image.new("RGB", (tw * cols, (th + 26) * rows), "white")
    for i, c in enumerate(saved):
        im = Image.open(c["file"])
        im.thumbnail((tw - 8, th - 8))
        x, y = (i % cols) * tw, (i // cols) * (th + 26)
        sheet.paste(im, (x + (tw - im.width) // 2, y + 26 + (th - im.height) // 2))
        ImageDraw.Draw(sheet).text((x + 6, y + 6), f'{c["n"]:02d}  {c["license"][:28]}', fill="black")
    sheet_path = os.path.join(os.path.dirname(folder), f"{slug}.jpg")
    sheet.save(sheet_path, quality=85)
    print(sheet_path)
    for c in saved:
        print(f'{c["n"]:02d}  {c["title"][:60]}  [{c["license"]}]')


if __name__ == "__main__":
    main()
