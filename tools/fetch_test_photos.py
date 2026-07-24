"""物体検出の動作確認用に Wikimedia Commons から実写画像を取得する（開発用）。

合成グラデーション画像では YOLO が何も検出しないため、実写でタグ付けを検証する。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path(__file__).parent.parent / "sample-photos"
UA = {"User-Agent": "PhotoFinder-dev/0.1 (local testing)"}

QUERIES = [
    ("common kingfisher perched", "real_kingfisher.jpg"),
    ("domestic cat portrait", "real_cat.jpg"),
    ("Passer montanus eurasian tree sparrow", "real_sparrow.jpg"),
    ("Ardea cinerea grey heron standing", "real_heron.jpg"),
    ("Corvus macrorhynchos large-billed crow", "real_crow.jpg"),
    ("Zosterops japonicus warbling white-eye", "real_mejiro.jpg"),
]


def commons_image_url(query: str) -> str | None:
    api = (
        "https://commons.wikimedia.org/w/api.php?action=query&format=json"
        "&generator=search&gsrnamespace=6&gsrlimit=5"
        "&gsrsearch=" + urllib.parse.quote(f"filetype:bitmap {query}") +
        "&prop=imageinfo&iiprop=url|mime&iiurlwidth=1600"
    )
    req = urllib.request.Request(api, headers=UA)
    data = json.load(urllib.request.urlopen(req, timeout=30))
    for page in (data.get("query", {}).get("pages") or {}).values():
        info = (page.get("imageinfo") or [{}])[0]
        if info.get("mime") == "image/jpeg":
            return info.get("thumburl") or info.get("url")
    return None


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for query, name in QUERIES:
        dst = OUT / name
        if dst.exists():
            print("exists:", name)
            continue
        url = commons_image_url(query)
        if not url:
            print("not found:", query)
            continue
        for attempt in range(4):  # Commons は連続取得で 429 を返すことがある
            try:
                req = urllib.request.Request(url, headers=UA)
                dst.write_bytes(urllib.request.urlopen(req, timeout=60).read())
                print(f"{name}: {dst.stat().st_size // 1024} KB  <- {url}")
                break
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == 3:
                    print(f"failed {name}: {e}")
                    break
                time.sleep(3 * (attempt + 1))
        time.sleep(1)


if __name__ == "__main__":
    main()
