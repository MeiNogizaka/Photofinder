"""PhotoFinder 差分インデクサ（サンプル実装）

使い方:
    python indexer.py --roots "D:\\Photos" "\\\\NAS\\photo" --ext jpg;jpeg;png;heic --data ./data

依存:
    pip install pillow pillow-heif rawpy piexif xxhash faiss-cpu onnxruntime numpy

設計は docs/design.md §2 に対応。ONNX モデル読み込み部はパスを差し替えて使う。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import xxhash

# ---------------------------------------------------------------- DB --------

DDL_PATH = Path(__file__).parent / "schema.sql"  # docs/data-schema.md の DDL を配置


def open_db(data_dir: Path) -> sqlite3.Connection:
    db = sqlite3.connect(data_dir / "photofinder.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.row_factory = sqlite3.Row
    if DDL_PATH.exists() and not _has_tables(db):
        db.executescript(DDL_PATH.read_text(encoding="utf-8"))
    return db


def _has_tables(db) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='photos'"
    ).fetchone() is not None


# ------------------------------------------------------------ 差分判定 ------

def fast_hash(path: Path, size: int) -> str:
    """先頭1MB + 末尾1MB + サイズの xxhash64。フル読み込みを回避。"""
    h = xxhash.xxh64()
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
        if size > (2 << 20):
            f.seek(-(1 << 20), os.SEEK_END)
            h.update(f.read())
    h.update(str(size).encode())
    return h.hexdigest()


@dataclass
class ScanResult:
    added: int = 0
    updated: int = 0
    moved: int = 0
    deleted: int = 0
    skipped: int = 0


def diff_scan(db: sqlite3.Connection, root_id: int, root: Path, exts: set[str]) -> tuple[ScanResult, list[tuple[int, Path]]]:
    """走査して差分のみジョブ化。返り値: (統計, [(photo_id, 絶対パス)] の抽出待ちリスト)"""
    known = {
        r["path"]: r
        for r in db.execute(
            "SELECT id, path, size, mtime FROM photos WHERE root_id=? AND deleted=0",
            (root_id,),
        )
    }
    res, jobs, seen = ScanResult(), [], set()

    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower().lstrip(".") not in exts:
            continue
        rel = str(p.relative_to(root))
        seen.add(rel)
        st = p.stat()
        rec = known.get(rel)

        if rec and rec["size"] == st.st_size and abs(rec["mtime"] - st.st_mtime) < 1:
            res.skipped += 1
            continue

        h = fast_hash(p, st.st_size)
        dup = db.execute(
            "SELECT id, root_id, path FROM photos WHERE xxhash=? AND deleted=0", (h,)
        ).fetchone()

        if dup and not (root / dup["path"]).exists():
            # 移動/リネーム: レコード付け替えのみ、ML 再処理なし
            db.execute(
                "UPDATE photos SET root_id=?, path=?, mtime=?, updated_at=datetime('now') WHERE id=?",
                (root_id, rel, st.st_mtime, dup["id"]),
            )
            res.moved += 1
        elif rec:
            db.execute(
                "UPDATE photos SET size=?, mtime=?, xxhash=?, index_state='pending' WHERE id=?",
                (st.st_size, st.st_mtime, h, rec["id"]),
            )
            jobs.append((rec["id"], p))
            res.updated += 1
        else:
            cur = db.execute(
                "INSERT INTO photos (root_id, path, size, mtime, xxhash, ext) VALUES (?,?,?,?,?,?)",
                (root_id, rel, st.st_size, st.st_mtime, h, p.suffix.lower().lstrip(".")),
            )
            jobs.append((cur.lastrowid, p))
            res.added += 1

    for rel in known.keys() - seen:
        db.execute("UPDATE photos SET deleted=1 WHERE id=?", (known[rel]["id"],))
        db.execute(
            "INSERT OR REPLACE INTO faiss_pending (photo_id, op) VALUES (?, 'remove')",
            (known[rel]["id"],),
        )
        res.deleted += 1

    db.commit()
    return res, jobs


# -------------------------------------------------------- 抽出パイプライン --

def extract_one(photo_id: int, path: str) -> dict:
    """ワーカプロセス内で 1 枚をフル解析。作業画像(長辺1024px)を全 ML で共有する。"""
    from ml_runtime import runtime  # ONNX セッションはプロセスごとに 1 度だけロード
    from PIL import Image, ImageOps

    img = _decode(Path(path))                      # HEIC/RAW 対応デコード
    img = ImageOps.exif_transpose(img)
    work = img.copy()
    work.thumbnail((1024, 1024))                   # ← 全タスク共有の作業画像

    exif = _extract_exif(Path(path))               # piexif で日時/GPS/カメラ
    thumb_path = _save_thumb(img, photo_id)        # 320px WebP

    out: dict = {
        "photo_id": photo_id,
        "width": img.width, "height": img.height,
        "exif": exif,
        "thumb": thumb_path,
        "embedding": runtime.siglip_image(work),   # 768d float32 L2正規化済み
        "phash": _phash64(work),
        "detections": runtime.yolo(work),          # [{label, conf, bbox}]
    }
    birds = [d for d in out["detections"] if d["label"] == "bird" and d["conf"] >= 0.35]
    if birds:
        out["bird_ids"] = [
            runtime.bird_classifier(_crop(work, b["bbox"], pad=1.2)) for b in birds
        ]
    if _looks_texty(work):                          # エッジ密度ヒューリスティック
        out["ocr"] = runtime.paddle_ocr(work)       # [{text, conf, bbox}]
    return out


def commit_extraction(db: sqlite3.Connection, r: dict, tokenizer) -> None:
    """1枚分の解析結果を単一トランザクションで確定。FTS と faiss_pending も同時更新。"""
    pid = r["photo_id"]
    e = r["exif"]
    db.execute(
        "UPDATE photos SET width=?, height=?, taken_at=?, phash=?, index_state='complete' WHERE id=?",
        (r["width"], r["height"], e.get("taken_at"), r["phash"], pid),
    )
    db.execute(
        """INSERT OR REPLACE INTO exif
           (photo_id, camera_make, camera_model, lens_model, focal_length_mm,
            f_number, shutter_speed, iso, gps_lat, gps_lon, gps_img_direction, raw_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pid, e.get("make"), e.get("model"), e.get("lens"), e.get("focal"),
         e.get("f"), e.get("ss"), e.get("iso"), e.get("lat"), e.get("lon"),
         e.get("direction"), e.get("raw_json")),
    )
    if e.get("lat") is not None:
        db.execute(
            "INSERT OR REPLACE INTO photo_rtree VALUES (?,?,?,?,?)",
            (pid, e["lat"], e["lat"], e["lon"], e["lon"]),
        )
        geo = reverse_geocode(e["lat"], e["lon"], e.get("direction"))  # ローカル POI DB
        if geo:
            db.execute(
                "INSERT OR REPLACE INTO geo VALUES (?,?,?,?,?,?,?)",
                (pid, geo["country"], geo["pref"], geo["city"],
                 geo.get("poi"), geo.get("poi_conf"), "osm_nearby"),
            )

    tag_words: list[str] = []
    db.execute("DELETE FROM detections WHERE photo_id=?", (pid,))
    for d in r["detections"]:
        cur = db.execute(
            "INSERT INTO detections (photo_id, label, conf, bbox) VALUES (?,?,?,?)",
            (pid, d["label"], d["conf"], ",".join(f"{v:.4f}" for v in d["bbox"])),
        )
        _upsert_tag(db, pid, d["label"], "auto", "yolo", d["conf"])
        tag_words.append(d["label"])
        for bird in r.get("bird_ids", []):
            db.execute(
                "INSERT OR REPLACE INTO bird_ids VALUES (?,?,?,?,?,0)",
                (cur.lastrowid, bird["ja"], bird["sci"], bird["conf"], bird["topk_json"]),
            )
            _upsert_tag(db, pid, bird["ja"], "species", "bird", bird["conf"])
            tag_words.append(bird["ja"])

    ocr_words = [o["text"] for o in r.get("ocr", [])]
    for o in r.get("ocr", []):
        db.execute(
            "INSERT INTO ocr_texts (photo_id, text, conf, bbox) VALUES (?,?,?,?)",
            (pid, o["text"], o["conf"], o.get("bbox", "")),
        )

    # FTS5: 分かち書き済みテキストを rowid=photo_id で登録
    db.execute("INSERT OR REPLACE INTO photos_fts (rowid, tags_text, ocr_text, place_text, caption) VALUES (?,?,?,?,?)",
               (pid, tokenizer(" ".join(tag_words)), tokenizer(" ".join(ocr_words)),
                tokenizer(_place_text(db, pid)), ""))

    db.execute(
        "INSERT OR REPLACE INTO faiss_pending (photo_id, op, vector) VALUES (?, 'add', ?)",
        (pid, r["embedding"].astype(np.float16).tobytes()),
    )
    db.commit()


# ------------------------------------------------------------ FAISS flush ---

def faiss_flush(db: sqlite3.Connection, data_dir: Path, dim: int = 768) -> None:
    """faiss_pending を FAISS に反映し、tmp→rename でアトミック保存。"""
    import faiss

    rows = db.execute("SELECT photo_id, op, vector FROM faiss_pending").fetchall()
    if not rows:
        return
    idx_path = data_dir / "vectors.faiss"
    if idx_path.exists():
        index = faiss.read_index(str(idx_path))
    else:
        index = faiss.IndexIDMap2(faiss.IndexHNSWFlat(dim, 32, faiss.METRIC_INNER_PRODUCT))

    adds = [(r["photo_id"], r["vector"]) for r in rows if r["op"] == "add"]
    if adds:
        vecs = np.stack([np.frombuffer(v, dtype=np.float16).astype(np.float32) for _, v in adds])
        index.add_with_ids(vecs, np.array([i for i, _ in adds], dtype=np.int64))
    # HNSW は物理削除不可 → remove は tombstone (photos.deleted) で検索時除外済み

    tmp = idx_path.with_suffix(".tmp")
    faiss.write_index(index, str(tmp))
    tmp.replace(idx_path)
    db.execute("DELETE FROM faiss_pending")
    db.commit()


# ------------------------------------------------------------------ main ----

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", nargs="+", required=True)
    ap.add_argument("--ext", default="jpg;jpeg;png;heic")
    ap.add_argument("--data", default="./data")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    args = ap.parse_args()

    data_dir = Path(args.data)
    (data_dir / "thumbs").mkdir(parents=True, exist_ok=True)
    db = open_db(data_dir)
    exts = set(args.ext.lower().split(";"))
    tokenizer = make_ja_tokenizer()  # SudachiPy/fugashi → 分かち書き関数

    for root_str in args.roots:
        root = Path(root_str)
        row = db.execute("SELECT id FROM roots WHERE path=?", (str(root),)).fetchone()
        root_id = row["id"] if row else db.execute(
            "INSERT INTO roots (path, ext_filter) VALUES (?,?)", (str(root), args.ext)
        ).lastrowid
        db.commit()

        t0 = time.time()
        stats, jobs = diff_scan(db, root_id, root, exts)
        print(f"[{root}] scan {time.time()-t0:.1f}s  +{stats.added} ~{stats.updated} "
              f"→{stats.moved} -{stats.deleted} ={stats.skipped}")

        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            done = 0
            for result in pool.map(extract_one, *zip(*[(i, str(p)) for i, p in jobs])) if jobs else []:
                commit_extraction(db, result, tokenizer)
                done += 1
                if done % 500 == 0:
                    faiss_flush(db, data_dir)
                    print(f"  ml {done}/{len(jobs)}")
        faiss_flush(db, data_dir)

    print("done.")


if __name__ == "__main__":
    main()


# --- 以下は本サンプルでは省略しているヘルパの契約（実装時に埋める） -----------
# _decode(path) -> PIL.Image        : jpg/png は Pillow、heic は pillow_heif、
#                                     raw は rawpy(埋め込みプレビュー優先: extract_thumb)
# _extract_exif(path) -> dict       : piexif。DateTimeOriginal→taken_at ISO8601、GPS 度分秒→10進
# _save_thumb(img, id) -> str       : 320px WebP q=80 を data/thumbs/{hash[:2]}/ へ
# _phash64(img) -> bytes            : 32x32 グレースケール → DCT 8x8 低周波 → 中央値ビット化
# _crop(img, bbox, pad) -> Image    : 正規化 bbox を pad 倍に広げて切り出し
# _looks_texty(img) -> bool         : Sobel エッジ密度 + 連結成分でテキスト存在を粗判定
# _upsert_tag(db,pid,name,kind,src,conf) : tags/photo_tags への UPSERT
# _place_text(db, pid) -> str       : geo テーブルから "京都府 京都市 鴨川デルタ" を合成
# reverse_geocode(lat,lon,dir)      : ローカル SQLite(R*Tree) の OSM POI 近傍検索
# make_ja_tokenizer() -> callable   : SudachiPy Mode.C で分かち書き（FTS5 用）
# ml_runtime.runtime                : ONNX セッション束(siglip_image/yolo/bird_classifier/paddle_ocr)
