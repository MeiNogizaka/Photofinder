"""PhotoFinder 検索 API（FastAPI サンプル実装）

起動:
    uvicorn search_api:app --host 127.0.0.1 --port 8686

docs/api-spec.md の GET /search, POST /search/by-image, GET /photos/{id} に対応。
ハイブリッド検索（FTS5 BM25 + SigLIP ベクトル → RRF 融合）の中核を示す。
"""
from __future__ import annotations

import io
import re
import sqlite3
from pathlib import Path

import faiss
import numpy as np
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

DATA = Path("./data")
DIM = 768
RRF_K = 60

app = FastAPI(title="PhotoFinder API")

# --- リソースはプロセスで 1 度だけロード -----------------------------------
_db = sqlite3.connect(DATA / "photofinder.db", check_same_thread=False)
_db.row_factory = sqlite3.Row
_index: faiss.Index = faiss.read_index(str(DATA / "vectors.faiss"))
# siglip_text/siglip_image は ONNX Runtime セッション（ここでは契約のみ）
from ml_runtime import runtime  # noqa: E402


# ============================================================ クエリ解析 ====

DATE_PATTERNS = [
    (re.compile(r"(\d{4})年"), lambda m: (f"{m[1]}-01-01", f"{m[1]}-12-31", f"{m[1]}年")),
    (re.compile(r"去年の?秋"), lambda m: _season_range(-1, "autumn")),
    (re.compile(r"今年の?春"), lambda m: _season_range(0, "spring")),
    # … 実装時に共通日付表現辞書へ拡張
]


def parse_query_filters(q: str) -> list[dict]:
    """日付・地名表現をフィルタ「候補」として抽出。適用は UI 側でユーザが決める。"""
    suggestions = []
    for pat, fn in DATE_PATTERNS:
        if m := pat.search(q):
            f, t, label = fn(m)
            suggestions.append({"type": "date", "label": label, "date_from": f, "date_to": t})
    for row in _db.execute(
        "SELECT DISTINCT prefecture FROM geo WHERE prefecture IS NOT NULL"
    ):
        name = row["prefecture"].removesuffix("府").removesuffix("県").removesuffix("都")
        if name and name in q:
            suggestions.append({"type": "place", "label": name, "place": name})
    return suggestions


# ============================================================ 検索コア ======

def fts_search(q: str, limit: int = 200) -> list[int]:
    """FTS5 BM25。クエリは登録時と同じ分かち書きを適用。"""
    tokens = runtime.tokenize_ja(q)
    if not tokens.strip():
        return []
    rows = _db.execute(
        """SELECT rowid FROM photos_fts WHERE photos_fts MATCH ?
           ORDER BY bm25(photos_fts, 3.0, 1.0, 2.0, 1.0) LIMIT ?""",
        (" OR ".join(f'"{t}"' for t in tokens.split()), limit),
    ).fetchall()
    return [r["rowid"] for r in rows]


def vec_search(vec: np.ndarray, k: int) -> list[int]:
    scores, ids = _index.search(vec.reshape(1, -1).astype(np.float32), k)
    return [int(i) for i in ids[0] if i != -1]


def rrf_fuse(*rankings: list[int]) -> list[int]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, pid in enumerate(ranking):
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores, key=scores.get, reverse=True)


def apply_sql_filters(ids: list[int], p: dict) -> list[int]:
    """フィルタを一括 SQL で適用し、入力順(=スコア順)を保って返す。"""
    if not ids:
        return []
    conds, args = ["p.id IN (%s)" % ",".join("?" * len(ids)), "p.deleted=0"], list(ids)
    if p.get("date_from"):
        conds.append("p.taken_at >= ?"); args.append(p["date_from"])
    if p.get("date_to"):
        conds.append("p.taken_at <= ?"); args.append(p["date_to"] + "T23:59:59")
    if p.get("camera"):
        ph = ",".join("?" * len(p["camera"]))
        conds.append(f"e.camera_model IN ({ph})"); args += p["camera"]
    if p.get("place"):
        conds.append("(g.prefecture LIKE ? OR g.city LIKE ? OR g.poi_name LIKE ?)")
        args += [f"%{p['place']}%"] * 3
    if p.get("bbox"):
        west, south, east, north = p["bbox"]
        conds.append("p.id IN (SELECT photo_id FROM photo_rtree "
                     "WHERE min_lat>=? AND max_lat<=? AND min_lon>=? AND max_lon<=?)")
        args += [south, north, west, east]
    for tag in p.get("tags", []):
        conds.append("EXISTS (SELECT 1 FROM photo_tags pt JOIN tags t ON t.id=pt.tag_id "
                     "WHERE pt.photo_id=p.id AND t.name=? AND pt.verified >= 0)")
        args.append(tag)

    rows = _db.execute(
        f"""SELECT p.id FROM photos p
            LEFT JOIN exif e ON e.photo_id=p.id
            LEFT JOIN geo  g ON g.photo_id=p.id
            WHERE {' AND '.join(conds)}""",
        args,
    ).fetchall()
    ok = {r["id"] for r in rows}
    return [i for i in ids if i in ok]


def hydrate(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    rows = _db.execute(
        f"""SELECT p.id, p.taken_at, p.width, p.height, p.ext,
                   (SELECT group_concat(t.name, ',') FROM photo_tags pt
                    JOIN tags t ON t.id = pt.tag_id
                    WHERE pt.photo_id = p.id AND pt.verified >= 0
                    ORDER BY pt.conf DESC LIMIT 3) AS top_tags
            FROM photos p WHERE p.id IN ({','.join('?' * len(ids))})""",
        ids,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [
        {
            "id": i,
            "thumb_url": f"/api/photos/{i}/thumb",
            "taken_at": by_id[i]["taken_at"],
            "width": by_id[i]["width"], "height": by_id[i]["height"],
            "ext": by_id[i]["ext"],
            "top_tags": (by_id[i]["top_tags"] or "").split(","),
        }
        for i in ids if i in by_id
    ]


# ============================================================ endpoints =====

@app.get("/api/search")
def search(
    q: str = "",
    date_from: str | None = None, date_to: str | None = None,
    place: str | None = None, camera: str | None = None,
    tags: str | None = None, bbox: str | None = None,
    mode: str = "hybrid", limit: int = Query(100, le=500), offset: int = 0,
):
    filters = {
        "date_from": date_from, "date_to": date_to, "place": place,
        "camera": camera.split(",") if camera else None,
        "tags": tags.split(",") if tags else [],
        "bbox": [float(v) for v in bbox.split(",")] if bbox else None,
    }
    has_filter = any(v for v in filters.values())
    k = 2000 if has_filter else max(limit * 3, 200)  # post-filtering 用に広めに取る

    if not q:  # フィルタのみブラウズ: 新しい順
        rows = _db.execute(
            "SELECT id FROM photos WHERE deleted=0 ORDER BY taken_at DESC LIMIT 5000"
        ).fetchall()
        fused = [r["id"] for r in rows]
        suggestions = []
    elif mode == "text":
        fused, suggestions = fts_search(q, k), parse_query_filters(q)
    elif mode == "vector":
        fused, suggestions = vec_search(runtime.siglip_text(q), k), parse_query_filters(q)
    else:  # hybrid
        fused = rrf_fuse(fts_search(q, 200), vec_search(runtime.siglip_text(q), k))
        suggestions = parse_query_filters(q)

    filtered = apply_sql_filters(fused, filters)
    page = filtered[offset : offset + limit]
    return {
        "items": hydrate(page),
        "total_estimate": len(filtered),
        "next_cursor": str(offset + limit) if len(filtered) > offset + limit else None,
        "suggested_filters": suggestions,
    }


@app.post("/api/search/by-image")
async def search_by_image(image: UploadFile = File(...), limit: int = 30):
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(Image.open(io.BytesIO(await image.read())).convert("RGB"))
    vec = runtime.siglip_image(img)
    cand_ids = vec_search(vec, 50)

    ph = runtime.phash64(img)
    exact, similar = [], []
    for pid in cand_ids:
        row = _db.execute(
            "SELECT phash, path, root_id FROM photos WHERE id=? AND deleted=0", (pid,)
        ).fetchone()
        if row is None:
            continue
        ham = _hamming(ph, row["phash"]) if row["phash"] else 64
        (exact if ham <= 10 else similar).append({"id": pid, "hamming": ham})

    return {
        "exact": [e | h for e, h in zip(hydrate([e["id"] for e in exact]), exact)],
        "similar": hydrate([s["id"] for s in similar])[: limit],
    }


@app.get("/api/photos/{photo_id}/thumb")
def thumb(photo_id: int):
    row = _db.execute("SELECT xxhash FROM photos WHERE id=?", (photo_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    path = DATA / "thumbs" / row["xxhash"][:2] / f"{row['xxhash']}.webp"
    return FileResponse(path, media_type="image/webp",
                        headers={"Cache-Control": "public, max-age=31536000, immutable",
                                 "ETag": row["xxhash"]})


def _hamming(a: bytes, b: bytes) -> int:
    return bin(int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).count("1")


def _season_range(year_offset: int, season: str) -> tuple[str, str, str]:
    from datetime import date
    y = date.today().year + year_offset
    ranges = {"spring": ("03-01", "05-31", "春"), "summer": ("06-01", "08-31", "夏"),
              "autumn": ("09-01", "11-30", "秋"), "winter": ("12-01", "02-28", "冬")}
    f, t, label = ranges[season]
    return f"{y}-{f}", f"{y}-{t}", f"{y}年{label}"
