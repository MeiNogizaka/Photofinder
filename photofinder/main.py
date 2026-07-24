"""PhotoFinder API サーバ (M2)。

起動:
    .venv\\Scripts\\python.exe -m uvicorn photofinder.main:app --port 8686

検索: SigLIP ベクトル検索 + キーワード検索 (ファイル名/タグ) の RRF 融合。
画像検索: SigLIP 類似 + pHash 再ランクで原本特定 (docs/design.md §3)。
"""
from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import fts, ml, scanner
from . import __version__
from .db import open_db
from .paths import data_dir
from .vectors import VectorStore

log = logging.getLogger("photofinder.main")
DATA_DIR = data_dir()
STATIC_DIR = Path(__file__).parent / "static"
RRF_K = 60
# Dockerfile が既定でENV設定する。コンテナにはデスクトップが無くファイル
# マネージャを開けないため、reveal系エンドポイント/UIボタンをここで判定して
# 出し分ける (GET /api/index/status の in_docker フィールド経由でフロントに伝える)
IN_DOCKER = os.environ.get("PHOTOFINDER_DOCKER") == "1"

app = FastAPI(
    title="PhotoFinder", version=__version__,
    license_info={"name": "AGPL-3.0", "url": "https://www.gnu.org/licenses/agpl-3.0.html"},
)
db = open_db(DATA_DIR)
vstore = VectorStore(DATA_DIR)
# db (LockedConnection) の内部ロックを共有する。個々の db.execute() は
# それ自体で直列化されるが、複数の execute() にまたがる書き込みシーケンスを
# 他スレッドの読み書きに割り込まれず一括りにするため、この with ブロックで
# 明示的に囲む (RLock なので内側の execute() が再入してもデッドロックしない)
_db_write = db.lock


def _reveal_in_file_manager(path: Path) -> None:
    """OS のファイルマネージャでファイルの場所を開く。

    Dockerコンテナにはデスクトップが無いため呼び出し前にIN_DOCKERで弾く
    (呼び出し元のエンドポイント参照)。bareメタル/venv でのローカル開発実行
    (macOS/Linuxデスクトップ) のみを対象とする — 旧app2/photofinderにあった
    Windows Explorer向けWin32シェルAPI実装は、配布がDocker専用になり
    Windows自体が対象プラットフォームで無くなったため削除した。
    """
    import sys
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])


# ================================================================ search ====

def _like_escape(s: str) -> str:
    """LIKE パターン用エスケープ。ユーザ入力の % _ をリテラル扱いにする。"""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_filters(date_from, date_to, camera, ext, tags,
                   place=None, bbox=None, posted=None) -> tuple[list, list]:
    conds, args = ["p.deleted=0", "p.index_state='complete'"], []
    if posted is not None:
        conds.append(
            ("EXISTS" if posted else "NOT EXISTS")
            + " (SELECT 1 FROM photo_posts pp WHERE pp.photo_id=p.id)")
    if date_from:
        conds.append("p.taken_at >= ?"); args.append(date_from)
    if date_to:
        conds.append("p.taken_at <= ?"); args.append(date_to + "T23:59:59")
    if camera:
        cams = camera.split(",")
        conds.append(f"e.camera_model IN ({','.join('?' * len(cams))})")
        args += cams
    if ext:
        exts = [x.lower().lstrip(".") for x in ext.split(",")]
        conds.append(f"p.ext IN ({','.join('?' * len(exts))})")
        args += exts
    if place:
        pat = f"%{_like_escape(place)}%"
        conds.append(
            "EXISTS (SELECT 1 FROM geo g WHERE g.photo_id=p.id AND ("
            "g.prefecture LIKE ? ESCAPE '\\' OR g.city LIKE ? ESCAPE '\\' "
            "OR g.poi_name LIKE ? ESCAPE '\\' OR g.poi_alt LIKE ? ESCAPE '\\'))")
        args += [pat, pat, pat, pat]
    if bbox:  # minLon,minLat,maxLon,maxLat (api-spec 準拠)
        west, south, east, north = bbox
        conds.append(
            "p.id IN (SELECT photo_id FROM photo_rtree "
            "WHERE min_lat>=? AND max_lat<=? AND min_lon>=? AND max_lon<=?)")
        args += [south, north, west, east]
    for tag in (tags.split(",") if tags else []):
        conds.append(
            "EXISTS (SELECT 1 FROM photo_tags pt JOIN tags t ON t.id=pt.tag_id "
            "WHERE pt.photo_id=p.id AND t.name=? AND pt.verified>=0)")
        args.append(tag)
    return conds, args


def _keyword_ranks(q: str, limit: int = 200) -> list[int]:
    """FTS5 (BM25, 分かち書き) を主とし、ファイル名 LIKE を補完として融合。"""
    fts_ids = fts.fts_ranks(db, q, limit)
    rows = db.execute(
        """SELECT p.id FROM photos p
           WHERE p.deleted=0 AND p.index_state='complete'
             AND p.path LIKE ? ESCAPE '\\'
           ORDER BY p.taken_at DESC LIMIT ?""",
        (f"%{_like_escape(q)}%", limit),
    ).fetchall()
    like_ids = [r["id"] for r in rows]
    if not like_ids:
        return fts_ids
    if not fts_ids:
        return like_ids
    return _rrf_fuse(fts_ids, like_ids)


def _rrf_fuse(*rankings: list[int]) -> list[int]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, pid in enumerate(ranking):
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores, key=lambda i: scores[i], reverse=True)


def _filter_ids(ids: list[int], conds: list, args: list) -> list[int]:
    """候補 id 群に SQL フィルタを適用し、入力順 (=スコア順) を保って返す。"""
    if not ids:
        return []
    ok: set[int] = set()
    for chunk_at in range(0, len(ids), 500):  # SQLite 変数上限対策
        chunk = ids[chunk_at:chunk_at + 500]
        where = " AND ".join(conds + [f"p.id IN ({','.join('?' * len(chunk))})"])
        ok.update(r["id"] for r in db.execute(
            f"SELECT p.id FROM photos p LEFT JOIN exif e ON e.photo_id=p.id "
            f"WHERE {where}", args + chunk))
    return [i for i in ids if i in ok]


def _hydrate(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    rows = db.execute(
        f"""SELECT p.id, p.path, p.taken_at, p.width, p.height, p.ext, p.xxhash,
                   (SELECT group_concat(t.name, ',') FROM (
                       SELECT t.name FROM photo_tags pt JOIN tags t ON t.id=pt.tag_id
                       WHERE pt.photo_id=p.id AND pt.verified>=0
                       ORDER BY pt.conf DESC LIMIT 3) t) AS top_tags
            FROM photos p WHERE p.id IN ({','.join('?' * len(ids))})""",
        ids,
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [
        {
            "id": i,
            # xxhash をクエリに含めて内容アドレス化する。photo_id は固定でも
            # 中身は再抽出で変わりうる (同一パスへの上書き等) ため、
            # id だけをキーにした URL に長期 immutable キャッシュを付けると
            # 内容が変わった後も古い画像がブラウザに残り続けてしまう
            "thumb_url": f"/api/photos/{i}/thumb?h={by_id[i]['xxhash']}",
            "taken_at": by_id[i]["taken_at"],
            "width": by_id[i]["width"], "height": by_id[i]["height"],
            "ext": by_id[i]["ext"],
            "filename": Path(by_id[i]["path"]).name,
            "top_tags": (by_id[i]["top_tags"] or "").split(",")
                        if by_id[i]["top_tags"] else [],
        }
        for i in ids if i in by_id
    ]


def _widen_k(conds: list, args: list) -> int:
    """FAISS検索のtop-kをフィルタの絞り込み強度に応じて200〜2000へ動的に広げる。

    以前はk=200固定で、docs/design.mdが意図していた「フィルタが狭いほど
    広いkを使う」という設計は未実装のまま乖離していた。フィルタ後の残存率
    (selectivity) が低いほど、FAISSの上位200件がフィルタでほぼ全滅して
    hitが極端に減る事態を避けるため広いkを使う。_build_filtersの結果
    (conds, args) をそのまま使い、空クエリbrowseパスと同じ軽量COUNTパターンを
    再利用する (新規クエリを1本追加するだけ)。
    """
    total = db.execute(
        "SELECT count(*) AS c FROM photos WHERE deleted=0 AND index_state='complete'"
    ).fetchone()["c"]
    if total == 0:
        return 200
    where = " AND ".join(conds)
    filtered = db.execute(
        f"SELECT count(*) AS c FROM photos p LEFT JOIN exif e ON e.photo_id=p.id "
        f"WHERE {where}", args).fetchone()["c"]
    selectivity = filtered / total
    if selectivity >= 0.5:
        k = 200
    elif selectivity >= 0.1:
        k = 500
    elif selectivity >= 0.02:
        k = 1000
    else:
        k = 2000
    return min(k, total)


@app.get("/api/search")
def search(
    q: str = "",
    date_from: str | None = None,
    date_to: str | None = None,
    camera: str | None = None,
    ext: str | None = None,
    tags: str | None = None,
    place: str | None = None,
    bbox: str | None = None,  # minLon,minLat,maxLon,maxLat
    posted: bool | None = None,  # X投稿リンクの有無で絞り込み (未指定なら全件)
    mode: str = Query("hybrid", pattern="^(hybrid|text|vector)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),  # ブラウズ時の時系列順
    limit: int = Query(100, le=500),
    offset: int = 0,
):
    bbox_vals = None
    if bbox:
        try:
            bbox_vals = [float(v) for v in bbox.split(",")]
            assert len(bbox_vals) == 4
        except (ValueError, AssertionError):
            raise HTTPException(422, "bbox must be minLon,minLat,maxLon,maxLat")
    conds, args = _build_filters(date_from, date_to, camera, ext, tags,
                                 place=place, bbox=bbox_vals, posted=posted)

    if not q:  # ブラウズ: 時系列 (order=desc 新しい順 / asc 古い順)
        where = " AND ".join(conds)
        direction = "ASC" if order == "asc" else "DESC"
        total = db.execute(
            f"SELECT count(*) AS c FROM photos p LEFT JOIN exif e ON e.photo_id=p.id "
            f"WHERE {where}", args).fetchone()["c"]
        rows = db.execute(
            f"SELECT p.id FROM photos p LEFT JOIN exif e ON e.photo_id=p.id "
            f"WHERE {where} ORDER BY p.taken_at {direction} LIMIT ? OFFSET ?",
            args + [limit, offset]).fetchall()
        items = _hydrate([r["id"] for r in rows])
        return {"items": items, "total_estimate": total,
                "next_cursor": str(offset + limit) if total > offset + limit else None,
                "suggested_filters": []}

    use_vec = mode in ("hybrid", "vector") and ml.runtime.available and vstore.count > 0
    vec_k = _widen_k(conds, args) if use_vec else 200
    vec_ids = [i for i, _ in vstore.search(ml.runtime.siglip_text(q), k=vec_k)] if use_vec else []
    kw_ids = _keyword_ranks(q) if mode in ("hybrid", "text") else []

    if mode == "vector":
        fused = vec_ids
    elif mode == "text" or not use_vec:
        fused = kw_ids
    else:
        fused = _rrf_fuse(kw_ids, vec_ids)

    filtered = _filter_ids(fused, conds, args)
    page = filtered[offset:offset + limit]
    return {
        "items": _hydrate(page),
        "total_estimate": len(filtered),
        "next_cursor": str(offset + limit) if len(filtered) > offset + limit else None,
        "suggested_filters": [],  # M4: クエリ解析で日付/地名候補を返す
    }


@app.post("/api/search/by-image")
async def search_by_image(image: UploadFile = File(...), limit: int = 30):
    """画像類似検索 + pHash 再ランクで原本特定 (docs/design.md §3.2)。"""
    if not ml.runtime.available:
        raise HTTPException(503, "ML models not installed (models/siglip)")
    from PIL import Image, ImageOps
    try:
        img = Image.open(io.BytesIO(await image.read()))
    except Exception:
        raise HTTPException(422, "cannot decode image")
    img = ImageOps.exif_transpose(img).convert("RGB")

    cands = vstore.search(ml.runtime.siglip_image(img), k=50)
    ph = ml.phash64(img)

    exact, similar = [], []
    for pid, score in cands:
        row = db.execute(
            "SELECT phash FROM photos WHERE id=? AND deleted=0", (pid,)).fetchone()
        if row is None:
            continue  # tombstone
        ham = ml.hamming(ph, row["phash"]) if row["phash"] else 64
        (exact if ham <= 10 else similar).append((pid, score, ham))

    exact.sort(key=lambda t: t[2])  # 同一候補はハミング距離の近い順
    exact_items = _hydrate([p for p, _, _ in exact])
    for it, (_, score, ham) in zip(exact_items, exact):
        it["score"], it["hamming"] = round(score, 4), ham
        it["path"] = photo_detail(it["id"])["path"]
    sim_items = _hydrate([p for p, _, _ in similar[:limit]])
    for it, (_, score, _) in zip(sim_items, similar):
        it["score"] = round(score, 4)
    return {"exact": exact_items, "similar": sim_items}


# ================================================================ photos ====

def _photo_or_404(photo_id: int):
    row = db.execute(
        """SELECT p.*, r.path AS root_path FROM photos p
           JOIN roots r ON r.id=p.root_id WHERE p.id=? AND p.deleted=0""",
        (photo_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "photo not found")
    return row


@app.get("/api/photos/{photo_id}")
def photo_detail(photo_id: int):
    p = _photo_or_404(photo_id)
    e = db.execute("SELECT * FROM exif WHERE photo_id=?", (photo_id,)).fetchone()
    tags = db.execute(
        """SELECT t.id, t.name, t.kind, pt.conf, pt.source, pt.verified
           FROM photo_tags pt JOIN tags t ON t.id=pt.tag_id
           WHERE pt.photo_id=? ORDER BY pt.verified DESC, pt.conf DESC""",
        (photo_id,),
    ).fetchall()
    detections = db.execute(
        """SELECT d.label, d.conf, d.bbox,
                  b.species_ja, b.species_sci, b.conf AS bird_conf,
                  b.topk_json, b.confirmed
           FROM detections d LEFT JOIN bird_ids b ON b.detection_id = d.id
           WHERE d.photo_id=? ORDER BY d.conf DESC""",
        (photo_id,),
    ).fetchall()
    geo_row = db.execute("SELECT * FROM geo WHERE photo_id=?", (photo_id,)).fetchone()
    ocr_rows = db.execute(
        "SELECT text, conf FROM ocr_texts WHERE photo_id=? ORDER BY conf DESC",
        (photo_id,),
    ).fetchall()
    post_rows = db.execute(
        """SELECT id, url, posted_at, caption_snippet, source, note, created_at
           FROM photo_posts WHERE photo_id=? ORDER BY created_at DESC""",
        (photo_id,),
    ).fetchall()
    exif = {}
    if e:
        exif = {
            "camera_make": e["camera_make"], "camera_model": e["camera_model"],
            "lens_model": e["lens_model"], "focal_length_mm": e["focal_length_mm"],
            "f_number": e["f_number"], "shutter_speed": e["shutter_speed"],
            "iso": e["iso"],
        }
        if e["gps_lat"] is not None:
            exif["gps"] = {"lat": e["gps_lat"], "lon": e["gps_lon"],
                           "direction": e["gps_img_direction"]}
    return {
        "id": p["id"],
        "path": str(Path(p["root_path"]) / p["path"]),
        "taken_at": p["taken_at"], "size": p["size"],
        "width": p["width"], "height": p["height"], "ext": p["ext"],
        "xxhash": p["xxhash"],
        "exif": exif,
        "geo": {
            "country": geo_row["country"], "prefecture": geo_row["prefecture"],
            "city": geo_row["city"], "poi_name": geo_row["poi_name"],
            "poi_source": geo_row["poi_source"],
        } if geo_row else None,
        "detections": [
            {"label": d["label"], "conf": d["conf"],
             "bbox": [float(v) for v in d["bbox"].split(",")],
             **({"bird": {
                 "species_ja": d["species_ja"] or None,
                 "species_sci": d["species_sci"],
                 "conf": d["bird_conf"],
                 "confirmed": bool(d["confirmed"]),
                 "topk": json.loads(d["topk_json"]) if d["topk_json"] else [],
             }} if d["species_sci"] else {})}
            for d in detections
        ],
        "ocr": [{"text": o["text"], "conf": o["conf"]} for o in ocr_rows],
        "tags": [dict(t) for t in tags],
        "posts": [dict(r) for r in post_rows],
    }


@app.get("/api/photos/{photo_id}/thumb")
def thumb(photo_id: int):
    p = _photo_or_404(photo_id)
    path = DATA_DIR / "thumbs" / p["xxhash"][:2] / f"{p['xxhash']}.webp"
    if not path.exists():
        raise HTTPException(404, "thumbnail not generated")
    return FileResponse(path, media_type="image/webp", headers={
        "Cache-Control": "public, max-age=31536000, immutable",
        "ETag": p["xxhash"],
    })


@app.get("/api/photos/{photo_id}/preview")
def preview(photo_id: int):
    path = scanner.get_or_make_preview(db, photo_id, DATA_DIR)
    if not path:
        raise HTTPException(404, "source file missing")
    # URL に ?h=xxhash が付き内容アドレス化されている (フロント側) ため、
    # thumb と同様に長期 immutable キャッシュにしても内容変更後に古い画像が
    # 残り続ける心配がない
    return FileResponse(path, media_type="image/webp", headers={
        "Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/api/photos/{photo_id}/file")
def original(photo_id: int):
    p = _photo_or_404(photo_id)
    src = Path(p["root_path"]) / p["path"]
    if not src.exists():
        raise HTTPException(404, "source file missing")
    return FileResponse(src, filename=src.name)


@app.post("/api/photos/{photo_id}/open-in-explorer")
def open_in_explorer(photo_id: int):
    if IN_DOCKER:
        raise HTTPException(501, "reveal is unavailable when running in Docker")
    p = _photo_or_404(photo_id)
    src = Path(p["root_path"]) / p["path"]
    if not src.exists():
        raise HTTPException(404, "source file missing")
    _reveal_in_file_manager(src)
    return {"ok": True}


@app.get("/api/export/options")
def export_options():
    """書き出しダイアログのフォント選択肢 (id/label) を返す。export.FONTS が唯一の定義元。"""
    from .export import DEFAULT_FONT, FONTS
    return {
        "fonts": [{"id": k, "label": v["label"]} for k, v in FONTS.items()],
        "default_font": DEFAULT_FONT,
    }


@app.post("/api/photos/{photo_id}/export")
def export(photo_id: int, body: dict = Body(default={})):
    """切り出し + 透かし + GPS除去で書き出し。原本は変更しない。"""
    from .export import export_photo
    p = _photo_or_404(photo_id)
    src = Path(p["root_path"]) / p["path"]
    if not src.exists():
        raise HTTPException(404, "source file missing")
    fmt = body.get("format", "jpeg")
    if fmt not in ("jpeg", "png", "webp"):
        raise HTTPException(422, "format must be jpeg|png|webp")
    # 書き出し先は exports 配下に限定 (任意パスへの書き込みを防ぐ)。
    # out_dir はサブフォルダ名としてのみ解釈する
    exports_root = (DATA_DIR / "exports").resolve()
    out_dir = exports_root
    if body.get("out_dir"):
        candidate = (exports_root / str(body["out_dir"])).resolve()
        try:
            candidate.relative_to(exports_root)
        except ValueError:
            raise HTTPException(403, "out_dir must be inside the exports folder")
        out_dir = candidate
    try:
        out = export_photo(
            src, out_dir,
            crop=body.get("crop"),
            watermark=body.get("watermark"),
            strip_metadata=body.get(
                "strip_metadata", body.get("strip_gps", True)),  # 旧名も受理
            fmt=fmt,
            quality=int(body.get("quality", 92)),
            max_edge=int(body["max_edge"]) if body.get("max_edge") else None,
        )
    except Exception as ex:
        raise HTTPException(500, f"export failed: {ex}")
    return {"out_path": str(out)}


@app.post("/api/reveal")
def reveal(body: dict = Body(...)):
    """書き出したファイルをエクスプローラで表示。exports 配下のみ許可。"""
    if IN_DOCKER:
        raise HTTPException(501, "reveal is unavailable when running in Docker")
    path = Path(body.get("path", ""))
    allowed = (DATA_DIR / "exports").resolve()
    try:
        resolved = path.resolve()
        resolved.relative_to(allowed)
    except (ValueError, OSError):
        raise HTTPException(403, "path not allowed")
    if not resolved.exists():
        raise HTTPException(404, "file not found")
    _reveal_in_file_manager(resolved)
    return {"ok": True}


# ================================================================== tags ====

@app.post("/api/photos/{photo_id}/tags")
def add_tag(photo_id: int, body: dict = Body(...)):
    _photo_or_404(photo_id)
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "tag name required")
    with _db_write:
        db.execute("INSERT OR IGNORE INTO tags (name, kind) VALUES (?, 'manual')", (name,))
        tag_id = db.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()["id"]
        db.execute(
            "INSERT OR REPLACE INTO photo_tags (photo_id, tag_id, source, verified) "
            "VALUES (?,?, 'user', 1)", (photo_id, tag_id))
        fts.update_fts(db, photo_id)
        db.commit()
    return {"id": tag_id, "name": name, "kind": "manual", "source": "user",
            "conf": None, "verified": 1}


@app.delete("/api/photos/{photo_id}/tags/{tag_id}")
def remove_tag(photo_id: int, tag_id: int):
    with _db_write:
        db.execute("DELETE FROM photo_tags WHERE photo_id=? AND tag_id=?",
                   (photo_id, tag_id))
        fts.update_fts(db, photo_id)
        db.commit()
    return {"ok": True}


@app.patch("/api/photos/{photo_id}/tags/{tag_id}")
def verify_tag(photo_id: int, tag_id: int, body: dict = Body(...)):
    v = body.get("verified")
    if v not in (1, -1, 0):  # 0 = 確定/否認の取り消し（未確定に戻す）
        raise HTTPException(422, "verified must be 1, -1 or 0")
    with _db_write:
        db.execute("UPDATE photo_tags SET verified=? WHERE photo_id=? AND tag_id=?",
                   (v, photo_id, tag_id))
        fts.update_fts(db, photo_id)  # 否認タグは検索から除外される
        db.commit()
    return {"ok": True}


@app.get("/api/tags")
def suggest_tags(q: str = ""):
    rows = db.execute(
        "SELECT id, name FROM tags WHERE name LIKE ? ESCAPE '\\' ORDER BY name LIMIT 20",
        (f"%{_like_escape(q)}%",),
    ).fetchall()
    return [dict(r) for r in rows]


# ============================================================== x posts ====
# 過去にXへ投稿した写真とツイートURLを紐づけ、再投稿(重複投稿)を防ぐための機能。
# X側の画像は切り抜き・透かし等でファイルが完全一致しないことが多いため、
# 重複判定は xxhash/pHash の完全一致ではなく SigLIP 埋め込みの類似度で行う。

SIMILAR_POST_MIN_SCORE = 0.75  # 暫定値。実運用で誤検知/見逃しを見ながら調整する


def _fetch_oembed(url: str) -> dict:
    """X公式oEmbedでキャプション等をベストエフォート取得する。

    ネットワーク不通・非対応URL・レート制限等で失敗しても呼び出し元が
    気にせず続行できるよう、例外を投げず空 dict を返す。
    """
    import re
    import urllib.parse
    import urllib.request
    try:
        api_url = "https://publish.twitter.com/oembed?url=" + urllib.parse.quote(url, safe="")
        with urllib.request.urlopen(api_url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}
    text = re.sub(r"<[^>]+>", " ", data.get("html", ""))
    text = re.sub(r"\s+", " ", text).strip()
    return {"caption_snippet": text[:200] or None}


def _similar_posted_photos(photo_id: int, limit: int = 5) -> list[dict]:
    """既に photo_posts を持つ写真の中から、対象写真とベクトル類似度が高いものを返す。

    切り抜き/透かし違いで完全一致しない「うっかり再投稿」を警告するための補助検索。
    ML未対応・未ベクトル化 (スキャン未完了等) の写真では空リストを返す
    (この機能自体が投稿リンクの登録をブロックすることはない)。
    """
    if not ml.runtime.available:
        return []
    vec = vstore.get_vector(photo_id)
    if vec is None:
        return []
    out = []
    for pid, score in vstore.search(vec, k=30):
        if pid == photo_id or score < SIMILAR_POST_MIN_SCORE:
            continue
        posts = db.execute(
            """SELECT pp.id, pp.url, pp.posted_at FROM photo_posts pp
               JOIN photos p ON p.id = pp.photo_id
               WHERE pp.photo_id=? AND p.deleted=0 ORDER BY pp.created_at""",
            (pid,)).fetchall()
        if not posts:
            continue
        item = _hydrate([pid])[0]
        item["score"] = round(score, 4)
        item["posts"] = [dict(r) for r in posts]
        out.append(item)
        if len(out) >= limit:
            break
    return out


@app.get("/api/photos/{photo_id}/posts/similar")
def similar_posted(photo_id: int):
    """この写真と類似度が高く、既にX投稿済みの写真を返す (重複投稿の警告用)。"""
    _photo_or_404(photo_id)
    return _similar_posted_photos(photo_id)


@app.post("/api/photos/{photo_id}/posts")
def add_post(photo_id: int, body: dict = Body(...)):
    _photo_or_404(photo_id)
    url = (body.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(422, "url must be an absolute http(s) URL")
    posted_at = (body.get("posted_at") or "").strip() or None
    note = (body.get("note") or "").strip() or None
    meta = _fetch_oembed(url)  # ベストエフォート。失敗しても登録は続行する
    with _db_write:
        cur = db.execute(
            "INSERT INTO photo_posts (photo_id, url, posted_at, caption_snippet, source, note) "
            "VALUES (?,?,?,?,'manual',?)",
            (photo_id, url, posted_at, meta.get("caption_snippet"), note))
        fts.update_fts(db, photo_id)
        db.commit()
    row = db.execute(
        "SELECT id, url, posted_at, caption_snippet, source, note, created_at "
        "FROM photo_posts WHERE id=?", (cur.lastrowid,)).fetchone()
    return {"post": dict(row), "duplicate_warnings": _similar_posted_photos(photo_id)}


@app.delete("/api/photos/{photo_id}/posts/{post_id}")
def remove_post(photo_id: int, post_id: int):
    with _db_write:
        db.execute("DELETE FROM photo_posts WHERE id=? AND photo_id=?", (post_id, photo_id))
        fts.update_fts(db, photo_id)
        db.commit()
    return {"ok": True}


# ======================================================== x archive import ==
# Xデータアーカイブ(zip)の一括取り込み (フェーズ2)。data/tweets.js の解析と
# SigLIP照合は archive_import.py に分離。ここでは HTTP 面 + photo_posts への
# 確定書き込みのみを扱う。候補一覧のレビュー確定は人間の判断を必須にする
# (自動で photo_posts へ書き込むと誤マッチのリンクが紛れ込みうるため)。

ARCHIVE_TMP_DIR = DATA_DIR / "tmp"
ARCHIVE_UPLOAD_CHUNK = 1 << 20  # 1MB


@app.post("/api/archive/import")
async def archive_import(
    file: UploadFile = File(...),
    date_from: str | None = Form(None),  # YYYY-MM-DD (投稿日で絞り込み、省略可)
    date_to: str | None = Form(None),
):
    from . import archive_import as ai
    # アップロード受付時点でロックを取る (try_acquire〜run_import完了まで
    # 一つのロックで直列化)。STATUS.running だけを見て後で書き込むと、2つの
    # 同時アップロードが両方このチェックを通過し、共有の一時ファイルを取り合う
    # 競合状態になりうるため
    if not ai.try_acquire():
        return {"started": False, "reason": "import already running"}
    # リクエストごとに一意な一時ファイル名にする (ロックで直列化した後も、
    # 前回の異常終了で残った同名ファイルとの衝突を避ける保険として)
    tmp_path = ARCHIVE_TMP_DIR / f"archive_import_{uuid.uuid4().hex}.zip"
    try:
        ARCHIVE_TMP_DIR.mkdir(parents=True, exist_ok=True)
        # zip をメモリに丸ごと読み込まず、チャンク単位でディスクへ流す
        # (Xアーカイブは動画込みで数GBになりうるため)
        with open(tmp_path, "wb") as f:
            while chunk := await file.read(ARCHIVE_UPLOAD_CHUNK):
                f.write(chunk)
        existing_urls = {r["url"] for r in db.execute("SELECT url FROM photo_posts")}

        def run():
            try:
                ai.run_import(tmp_path, vstore, existing_urls,
                              date_from=date_from or None, date_to=date_to or None)
            finally:
                tmp_path.unlink(missing_ok=True)
        threading.Thread(target=run, daemon=True).start()
    except Exception:
        # スレッドが正常に起動した後は run_import() 側の finally が
        # ロック解放とtmpファイル削除を担う。ここに来るのはスレッド起動前
        # (zip書き込み中のI/Oエラー・DB読み取り失敗等) に限られるため、
        # このリクエスト自身でロックとtmpファイルの後始末をする
        ai.release()
        tmp_path.unlink(missing_ok=True)
        raise
    return {"started": True}


@app.get("/api/archive/import-status")
def archive_import_status():
    from . import archive_import as ai
    snap = ai.STATUS.snapshot()
    if snap["candidates"]:
        # thumb_url はここで内容アドレス化 (?h=xxhash) して付与する。
        # archive_import.py 側は main.py の _hydrate に依存させない (層を分ける)
        thumbs = {h["id"]: h["thumb_url"]
                  for h in _hydrate([c["photo_id"] for c in snap["candidates"]])}
        for c in snap["candidates"]:
            c["thumb_url"] = thumbs.get(c["photo_id"])
    return snap


ARCHIVE_SUGGEST_BUFFER_DAYS = 3  # 未確認のまま取りこぼした古い候補を拾えるよう余裕を持たせる


@app.get("/api/archive/suggested-date-from")
def archive_suggested_date_from():
    """次回のXアーカイブ取り込みで date_from に使う日付の目安を返す。

    Xのアーカイブ書き出しは常に全期間のエクスポートで、サーバ側に差分取得の
    手段がない。そのためアプリ側で「これまでにリンク済みの投稿のうち
    最新の投稿日」を記憶しておき、そこから数日分のバッファを引いた日付を
    目安として提示する (バッファがないと、レビュー未確定のまま残っていた
    境界付近の古い候補を次回取りこぼす恐れがあるため)。あくまで目安であり、
    確実に全件拾いたい場合は期間指定なしで取り込むこと。
    """
    row = db.execute(
        "SELECT MAX(posted_at) AS m FROM photo_posts WHERE posted_at IS NOT NULL"
    ).fetchone()
    if not row or not row["m"]:
        return {"date_from": None}
    try:
        latest = datetime.fromisoformat(row["m"])
    except ValueError:
        return {"date_from": None}  # posted_at がISO8601でない (パース失敗の生値) 場合は諦める
    suggested = (latest - timedelta(days=ARCHIVE_SUGGEST_BUFFER_DAYS)).date().isoformat()
    return {"date_from": suggested, "latest_linked_posted_at": row["m"]}


@app.post("/api/archive/import/confirm")
def archive_import_confirm(body: dict = Body(...)):
    """レビュー画面で確認した候補だけを photo_posts へ書き込む。

    body: {"accepted": [{"photo_id": int, "tweet_id": str}, ...]}
    """
    from . import archive_import as ai
    accepted = body.get("accepted") or []
    by_key = {(c.photo_id, c.tweet_id): c for c in ai.STATUS.candidates}
    # 同一ツイートの複数画像が同じローカル写真にマッチするケースがあり、
    # (photo_id, tweet_id) が重複するチェック項目が送られてくることがある。
    # 重複挿入 (同じ投稿を指す photo_posts 行が2本できる) を避けるため一意化する
    seen: set[tuple[int, str]] = set()
    written = 0
    with _db_write:
        for item in accepted:
            key = (item.get("photo_id"), str(item.get("tweet_id")))
            if key in seen:
                continue
            c = by_key.get(key)
            if not c:
                continue
            seen.add(key)
            db.execute(
                "INSERT INTO photo_posts (photo_id, url, posted_at, caption_snippet, source) "
                "VALUES (?,?,?,?,'archive')",
                (c.photo_id, c.url, c.posted_at, c.text[:200] or None))
            fts.update_fts(db, c.photo_id)
            written += 1
        db.commit()
    ai.STATUS.candidates = []  # 確定後は候補をクリアして二重登録・再表示を防ぐ
    ai.STATUS.phase = "idle"
    return {"written": written}


@app.post("/api/archive/import/dismiss")
def archive_import_dismiss():
    """レビュー結果を何も確定せず破棄する。"""
    from . import archive_import as ai
    ai.STATUS.candidates = []
    ai.STATUS.phase = "idle"
    return {"ok": True}


# ================================================================= roots ====

@app.get("/api/roots")
def list_roots():
    rows = db.execute("SELECT * FROM roots").fetchall()
    counts = {
        r["root_id"]: r["c"]
        for r in db.execute(
            "SELECT root_id, count(*) AS c FROM photos WHERE deleted=0 GROUP BY root_id")
    }
    return [dict(r) | {"photo_count": counts.get(r["id"], 0)} for r in rows]


@app.post("/api/roots")
def add_root(body: dict = Body(...)):
    path = (body.get("path") or "").strip()
    if not path or not Path(path).is_dir():
        raise HTTPException(422, f"folder not found: {path}")
    ext_filter = body.get("ext_filter", "jpg;jpeg;png;heic")
    recursive = 0 if body.get("recursive") is False else 1  # 既定は再帰
    with _db_write:
        cur = db.execute(
            "INSERT OR IGNORE INTO roots (path, ext_filter, recursive) VALUES (?,?,?)",
            (path, ext_filter, recursive))
        db.commit()
    if cur.rowcount == 0:
        raise HTTPException(409, "root already exists")
    # 追加フォルダを即インデックス。スキャン実行中なら完了を待って続けて実行する
    _start_scan_thread(cur.lastrowid, wait=True)
    return {"id": cur.lastrowid, "path": path, "ext_filter": ext_filter,
            "recursive": recursive}


@app.delete("/api/roots/{root_id}")
def delete_root(root_id: int):
    """フォルダをインデックスから除外する。写真ファイルには一切触れない。

    インデックス上のレコード (photos + 付随データ) は完全削除する。
    tombstone (deleted=1) では photos が roots を参照したままになり
    FK 制約で root 行を消せないため。ベクトルは削除直後にこの関数内で
    再構築して物理的に除去する (HNSW は remove 非対応で放置すると
    死んだ id が類似検索の上位k件に混入し精度が落ちるため)。
    """
    if not db.execute("SELECT 1 FROM roots WHERE id=?", (root_id,)).fetchone():
        raise HTTPException(404, "root not found")
    if scanner.STATUS.running:
        # スキャン中に消すと抽出スレッドと競合する (存在しない photo への書き込み等)
        raise HTTPException(409, "scan running - try again after it finishes")
    with _db_write:
        ids = [r["id"] for r in db.execute(
            "SELECT id FROM photos WHERE root_id=?", (root_id,))]
        for chunk_at in range(0, len(ids), 500):
            chunk = ids[chunk_at:chunk_at + 500]
            ph = ",".join("?" * len(chunk))
            # FK CASCADE の無い付随テーブルを先に掃除
            db.execute(f"DELETE FROM photos_fts WHERE rowid IN ({ph})", chunk)
            db.execute(f"DELETE FROM photo_rtree WHERE photo_id IN ({ph})", chunk)
            db.execute(f"DELETE FROM faiss_pending WHERE photo_id IN ({ph})", chunk)
        # exif/geo/detections/bird_ids/ocr_texts/photo_tags は ON DELETE CASCADE
        db.execute("DELETE FROM photos WHERE root_id=?", (root_id,))
        db.execute("DELETE FROM roots WHERE id=?", (root_id,))
        db.commit()
    _rebuild_vectors()
    return {"ok": True, "removed_photos": len(ids)}


# ================================================================ settings ==

# 真偽値として扱う設定キーの一覧 (PATCH で受け付けるキーもここで制限)
BOOL_SETTINGS = {"scan_on_startup", "backup_auto"}


def get_setting(key: str, default: str = "") -> str:
    row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


@app.get("/api/settings")
def settings():
    rows = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM app_settings")}
    # BOOL_SETTINGS は全て未設定時「有効」がデフォルト (get_setting(key, "1") と揃える)。
    # rows に無いキーを単に省略すると、フロントの初期表示が false 相当になり
    # 実際のデフォルト挙動 (有効) と食い違ってしまうため、ここで明示的に補う
    result = {k: rows.get(k, "1") == "1" for k in BOOL_SETTINGS}
    result.update({k: v for k, v in rows.items() if k not in BOOL_SETTINGS})
    return result


@app.patch("/api/settings")
def patch_settings(body: dict = Body(...)):
    with _db_write:
        for key, value in body.items():
            if key not in BOOL_SETTINGS:
                raise HTTPException(422, f"unknown setting: {key}")
            db.execute("INSERT OR REPLACE INTO app_settings VALUES (?,?)",
                       (key, "1" if value else "0"))
        db.commit()
    return settings()


@app.post("/api/shutdown")
def shutdown():
    """アプリを終了する (exe化ステップ5: console=Falseでウィンドウを閉じる
    終了操作ができないため、UIから明示的に終了する手段として追加)。

    レスポンスを確実に返してからプロセスを終了させるため、別スレッドで
    少し待ってから os._exit する。SQLite は WAL モードで途中終了に対して
    安全なため、明示的なクリーンアップ(close等)は不要
    """
    def _exit_soon():
        time.sleep(0.3)
        os._exit(0)
    threading.Thread(target=_exit_soon, daemon=True).start()
    return {"ok": True}


# ================================================================= index ====

def _start_scan_thread(root_id: int | None = None, wait: bool = False) -> None:
    def run():
        # スキャン専用の接続を使う。API ハンドラと接続を共有すると
        # 互いのトランザクションに commit が割り込む (WAL でも接続内は共有) ため
        sdb = open_db(DATA_DIR)
        try:
            scanner.scan_all(sdb, DATA_DIR, root_id, vstore, wait=wait)
        finally:
            sdb.close()
    threading.Thread(target=run, daemon=True).start()


@app.on_event("startup")
def scan_on_startup():
    """スキャンは起動時 (本設定で無効化可) と手動ボタンのみ (ユーザ決定の仕様)。"""
    if get_setting("scan_on_startup", "1") == "1":
        _start_scan_thread()


@app.on_event("startup")
def backup_on_startup():
    """自動バックアップ: 起動時に前回から7日以上経過していればスナップショット。"""
    from .backup import auto_backup_if_due
    if get_setting("backup_auto", "1") == "1":
        threading.Thread(
            target=auto_backup_if_due, args=(DATA_DIR,), daemon=True
        ).start()


# ================================================================ backup ====

@app.get("/api/backup/status")
def backup_status():
    from .backup import INTERVAL_DAYS, KEEP_GENERATIONS, list_snapshots
    return {
        "enabled": get_setting("backup_auto", "1") == "1",
        "interval_days": INTERVAL_DAYS,
        "keep_generations": KEEP_GENERATIONS,
        "snapshots": list_snapshots(DATA_DIR),
    }


@app.post("/api/backup/snapshot")
def backup_snapshot(body: dict = Body(default={})):
    from .backup import snapshot
    try:
        result = snapshot(DATA_DIR)  # 専用接続で実行するため _db_write 不要
    except Exception as ex:
        raise HTTPException(500, f"backup failed: {ex}")
    if body.get("rebuild"):  # バックアップ時オプション: ベクトル索引の再構築
        result["rebuild"] = _rebuild_vectors()
    return result


@app.post("/api/export/dataset")
def export_dataset(body: dict = Body(default={})):
    """人手タグ付け済み写真をJSONL+画像zipとしてエクスポート (教師/評価データセット化)。

    photo_tags.verified!=0 (人手確定/否認済み) のタグを持つ写真のみが対象
    (dataset_export.py 参照)。数百〜数千枚規模を想定し、既存の単体写真書き出し
    と同じ直接応答パターンで同期即時ダウンロードとして返す。
    """
    from .dataset_export import build_dataset_zip
    exports_root = (DATA_DIR / "exports").resolve()
    exports_root.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = exports_root / f"dataset-{ts}.zip"
    try:
        summary = build_dataset_zip(
            db, DATA_DIR, out_path,
            include_negatives=body.get("include_negatives", True),
            include_bird_detail=body.get("include_bird_detail", True),
        )
    except Exception as ex:
        out_path.unlink(missing_ok=True)
        raise HTTPException(500, f"dataset export failed: {ex}")
    if summary["photos"] == 0:
        out_path.unlink(missing_ok=True)
        raise HTTPException(404, "no verified (confirmed/rejected) tags to export")
    return FileResponse(out_path, media_type="application/zip", filename=out_path.name)


# ================================================================== geo =====

@app.get("/api/geo/poi-status")
def poi_status():
    """POI データの取得状況 (設定画面で表示)。"""
    from .geo import poi_status as _status
    return _status()


@app.post("/api/geo/refresh-poi")
def refresh_poi():
    """POI データ更新後、GPS を持つ全写真に場所名を再適用する (画像の再解析なし)。

    運用フロー: 設定画面で都道府県を取得/更新 (または tools/build_poi_db.py) → 本エンドポイント。
    """
    from .fts import update_fts
    from .geo import invalidate_poi_db, reverse_geocode
    invalidate_poi_db()  # 更新された poi.db を開き直す

    rows = db.execute(
        """SELECT e.photo_id, e.gps_lat, e.gps_lon, e.gps_img_direction
           FROM exif e JOIN photos p ON p.id = e.photo_id
           WHERE p.deleted = 0 AND e.gps_lat IS NOT NULL""").fetchall()
    updated = 0
    # reverse_geocode (CPU + poi.db I/O) は _db_write ロックの外で1枚ずつ呼び、
    # 書き込みだけを写真ごとに短く lock する。全件を1つの with _db_write で
    # 囲むと、写真が多い場合に数分間 DB 全体 (サムネイル表示・検索含む) が
    # 固まる上、途中でクラッシュすると commit 前の分が全て失われる
    for r in rows:
        cur = db.execute("SELECT poi_source FROM geo WHERE photo_id=?",
                         (r["photo_id"],)).fetchone()
        if cur and cur["poi_source"] == "manual":
            continue  # 手動修正は保持
        g = reverse_geocode(r["gps_lat"], r["gps_lon"], r["gps_img_direction"])
        if not g:
            continue
        with _db_write:
            db.execute(
                "INSERT OR REPLACE INTO geo "
                "(photo_id, country, prefecture, city, poi_name, poi_conf, "
                " poi_alt, poi_source) VALUES (?,?,?,?,?,?,?,'osm_nearby')",
                (r["photo_id"], g["country"], g["prefecture"], g["city"],
                 g["poi_name"], g["poi_conf"], g["poi_alt"]))
            update_fts(db, r["photo_id"])
            db.commit()
        updated += 1
    return {"photos_with_gps": len(rows), "updated": updated}


@app.get("/api/geo/prefectures")
def geo_prefectures():
    """都道府県名の一覧 (POI取得フォームのドロップダウン用)。"""
    from .poi_fetch import PREF_ISO
    return list(PREF_ISO.keys())


@app.get("/api/geo/missing-prefectures")
def missing_prefectures():
    """写真の撮影地に含まれるが POI データが未取得の都道府県 (件数付き)。

    「北海道の写真があるがデータがない」提案バナー用。
    """
    from .poi_fetch import open_poi_db
    have = db.execute(
        """SELECT g.prefecture, count(*) AS c FROM geo g
           JOIN photos p ON p.id = g.photo_id
           WHERE p.deleted=0 AND g.prefecture IS NOT NULL AND g.prefecture != ''
           GROUP BY g.prefecture ORDER BY c DESC"""
    ).fetchall()
    pdb = open_poi_db()
    try:
        fetched = {r["pref"] for r in pdb.execute("SELECT pref FROM poi_meta")}
    finally:
        pdb.close()
    return [{"prefecture": r["prefecture"], "photo_count": r["c"]}
            for r in have if r["prefecture"] not in fetched]


@app.post("/api/geo/poi/fetch")
def fetch_poi(body: dict = Body(...)):
    """都道府県のPOIをOverpass APIから取得しDBへ格納 (バックグラウンド実行)。"""
    from . import poi_fetch
    pref = (body.get("pref") or "").strip()
    if pref not in poi_fetch.PREF_ISO:
        raise HTTPException(422, f"unknown prefecture: {pref}")
    if poi_fetch.STATUS.running:
        return {"started": False, "reason": "fetch already running"}
    threading.Thread(target=poi_fetch.fetch_and_store, args=(pref,), daemon=True).start()
    return {"started": True, "pref": pref}


@app.get("/api/geo/poi/fetch-status")
def poi_fetch_status():
    from . import poi_fetch
    return poi_fetch.STATUS.snapshot()


@app.delete("/api/geo/poi/prefecture/{pref}")
def delete_poi_prefecture(pref: str):
    """指定都道府県のPOIデータを削除する (取得のやり直し・不要データの整理用)。"""
    from .geo import invalidate_poi_db
    from .poi_fetch import delete_pref, open_poi_db
    pdb = open_poi_db()
    try:
        n = delete_pref(pdb, pref)
    finally:
        pdb.close()
    invalidate_poi_db()
    return {"removed": n}


@app.get("/api/geo/poi/search")
def search_poi(q: str = "", limit: int = 20):
    """POI名でオートコンプリート検索 (写真の場所編集で使用)。"""
    from .poi_fetch import open_poi_db, search_pois
    pdb = open_poi_db()
    try:
        return search_pois(pdb, q, limit) if q else []
    finally:
        pdb.close()


@app.get("/api/geo/poi/custom")
def list_custom_poi():
    """ユーザが手動追加したカスタム地点の一覧 (設定画面の管理用)。"""
    from .poi_fetch import open_poi_db
    pdb = open_poi_db()
    try:
        rows = pdb.execute(
            "SELECT id, name, lat, lon FROM pois WHERE pref='manual' ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        pdb.close()


@app.post("/api/geo/poi")
def add_poi(body: dict = Body(...)):
    """ユーザによるカスタム地点の手動追加。近傍検索(poi_lookup)に自動的に反映される。"""
    from .geo import invalidate_poi_db
    from .poi_fetch import add_manual_poi, open_poi_db
    name = (body.get("name") or "").strip()
    try:
        lat, lon = float(body["lat"]), float(body["lon"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, "lat/lon must be numbers")
    if not name:
        raise HTTPException(422, "name required")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(422, "lat/lon out of range")
    pdb = open_poi_db()
    try:
        poi_id = add_manual_poi(pdb, name, lat, lon)
    finally:
        pdb.close()
    invalidate_poi_db()
    return {"id": poi_id, "name": name, "lat": lat, "lon": lon}


@app.delete("/api/geo/poi/{poi_id}")
def delete_poi_endpoint(poi_id: int):
    from .geo import invalidate_poi_db
    from .poi_fetch import delete_poi, open_poi_db
    pdb = open_poi_db()
    try:
        ok = delete_poi(pdb, poi_id)
    finally:
        pdb.close()
    if not ok:
        raise HTTPException(404, "poi not found")
    invalidate_poi_db()
    return {"ok": True}


@app.patch("/api/photos/{photo_id}/place")
def set_photo_place(photo_id: int, body: dict = Body(...)):
    """写真1枚の場所名を手動で上書き/解除する。

    poi_name を指定 → geo.poi_source='manual' として固定（以後の POI 再適用で上書きされない）。
    poi_name を null/省略 → 自動判定に戻す（現在の GPS で reverse_geocode を再実行）。
    """
    from .fts import update_fts
    from .geo import reverse_geocode
    p = _photo_or_404(photo_id)
    poi_name = body.get("poi_name")
    # reverse_geocode (CPU + poi.db I/O) は _db_write ロックの外で行う。
    # ロックを持ったまま呼ぶと他のAPIリクエスト (サムネイル表示・検索等) が
    # その間ブロックされる (main.py の db は全リクエストで共有する1接続のため)
    g = None
    if not poi_name:
        e = db.execute("SELECT gps_lat, gps_lon, gps_img_direction FROM exif "
                       "WHERE photo_id=?", (photo_id,)).fetchone()
        if e and e["gps_lat"] is not None:
            g = reverse_geocode(e["gps_lat"], e["gps_lon"], e["gps_img_direction"])
    with _db_write:
        if poi_name:
            existing = db.execute(
                "SELECT country, prefecture, city FROM geo WHERE photo_id=?",
                (photo_id,)).fetchone()
            db.execute(
                "INSERT INTO geo (photo_id, country, prefecture, city, poi_name, "
                " poi_conf, poi_alt, poi_source) VALUES (?,?,?,?,?,1.0,NULL,'manual') "
                "ON CONFLICT(photo_id) DO UPDATE SET poi_name=excluded.poi_name, "
                " poi_conf=1.0, poi_alt=NULL, poi_source='manual'",
                (photo_id, existing["country"] if existing else None,
                 existing["prefecture"] if existing else None,
                 existing["city"] if existing else None, poi_name.strip()))
        elif g:
            db.execute(
                "INSERT OR REPLACE INTO geo (photo_id, country, prefecture, "
                " city, poi_name, poi_conf, poi_alt, poi_source) "
                "VALUES (?,?,?,?,?,?,?,'osm_nearby')",
                (photo_id, g["country"], g["prefecture"], g["city"],
                 g["poi_name"], g["poi_conf"], g["poi_alt"]))
        else:
            # GPSが無い、またはGPSはあっても自動判定に失敗した場合。
            # どちらのケースでも手動設定(poi_source='manual')を確実に解除する
            # (以前はGPSありかつ自動判定失敗の場合だけ何もせず手動設定が
            # 残ってしまい、「自動に戻す」を押しても反映されなかった)
            db.execute(
                "UPDATE geo SET poi_name=NULL, poi_conf=NULL, poi_source=NULL "
                "WHERE photo_id=?", (photo_id,))
        update_fts(db, photo_id)
        db.commit()
    row = db.execute("SELECT * FROM geo WHERE photo_id=?", (photo_id,)).fetchone()
    return dict(row) if row else {"photo_id": photo_id, "poi_name": None}


@app.post("/api/index/scan")
def start_scan(body: dict = Body(default={})):
    """手動スキャン開始。root_id 指定でそのフォルダだけ再スキャン。"""
    if scanner.STATUS.running:
        return {"started": False, "reason": "scan already running"}
    root_id = body.get("root_id")
    if root_id is not None:
        if not db.execute("SELECT 1 FROM roots WHERE id=?", (root_id,)).fetchone():
            raise HTTPException(404, "root not found")
    _start_scan_thread(root_id)
    return {"started": True, "root_id": root_id}


@app.post("/api/index/cancel")
def cancel_scan():
    """実行中スキャンに停止を要求する。処理済みは保持、未処理は次回スキャンで再開。

    未処理の写真は index_state='pending'（新規/更新）または旧 ml_version のまま
    残るため、次のスキャンで自動的に再キューされる (中断＝一時停止)。
    """
    if not scanner.STATUS.running:
        return {"cancelled": False, "reason": "no scan running"}
    scanner.STATUS.cancel_requested = True
    return {"cancelled": True}


def _rebuild_vectors() -> dict:
    """未削除写真の id 集合でベクトル索引を作り直す (重複・不要ベクトル除去)。

    スキャン中は faiss_pending flush と競合し未反映ベクトルを失いうるため実行しない。
    バックアップの付随オプションからも呼ばれるため、ここでは例外にせず
    スキップを示す dict を返す（バックアップ自体は成功させたいため）。
    呼び出し元の意図次第で 409 にするか無視するかを選べる。
    """
    if scanner.STATUS.running:
        return {"skipped": True, "reason": "scan running"}
    valid = {r["id"] for r in db.execute("SELECT id FROM photos WHERE deleted=0")}
    return vstore.rebuild(valid)


@app.post("/api/index/rebuild-vectors")
def rebuild_vectors():
    result = _rebuild_vectors()
    if result.get("skipped"):
        raise HTTPException(409, "scan running - try again after it finishes")
    return result


@app.get("/api/index/status")
def index_status():
    from .detector import detector
    total = db.execute(
        "SELECT count(*) AS c FROM photos WHERE deleted=0 AND index_state='complete'"
    ).fetchone()["c"]
    return scanner.STATUS.snapshot() | {
        "indexed_total": total,
        "vectors": vstore.count,
        "ml_available": ml.runtime.available,
        # 実際に使われている実行プロバイダ (CUDAExecutionProvider=GPU / CPUExecutionProvider=CPU)。
        # 各モデルは初回使用時まで遅延ロードのため、未使用のうちは null (UIは「未使用」表示)
        "providers": {
            "siglip_text": ml.runtime.text_provider,
            "siglip_vision": ml.runtime.vision_provider,
            "yolo": detector.active_provider,
        },
        # コンテナ実行かどうか。フロントはこれでreveal/エクスプローラ関連UIの表示を切り替える
        "in_docker": IN_DOCKER,
    }


# ================================================================ static ====

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
