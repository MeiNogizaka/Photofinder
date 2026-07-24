"""差分スキャナ + 抽出パイプライン (M1: EXIF + サムネイル)。

docs/design.md §2 の設計に対応。M1 では ML 手順 (embedding/detect/ocr) は
extract_one 内のフックとして空実装にしてあり、M2 以降で差し替える。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import xxhash
from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:  # HEIC 非対応環境でも動作は継続
    pass

from . import raw_utils

log = logging.getLogger("photofinder.scanner")

THUMB_EDGE = 320
PREVIEW_EDGE = 1600
# 抽出パイプラインの版。上げると全写真が次回スキャンでバックフィルされる
# v7: 野鳥種リスト拡充 (外来種/展示種を追加、species_ja.py)
# v8: 物体検出モデルを YOLOv8n → YOLOv8m に変更 (精度優先、detector.py)
# v9: FTS 分かち書きに NFKC 正規化を追加 (fts.py)。v8 以前に索引済みの写真の
#     全角トークンをクエリ側の正規化と一致させるため全写真を再索引する
# v10: GPU (CUDAExecutionProvider) 対応 + 物体検出を YOLOv8m → YOLOv8x、
#      埋め込みを SigLIP base (768d/256px) → SigLIP2 so400m (1152d/384px) に変更
#      (精度優先、detector.py/ml.py)。次元変更で vectors.faiss も作り直しになる
#      (vectors.py の次元不一致チェック参照) ため全写真の再エンコードが必要
ML_VERSION = 10
BIRD_CROP_PAD = 1.2  # bird bbox をこの倍率に広げて切り出す（周辺文脈を含める）


# ------------------------------------------------------------ 進捗状態 -----

@dataclass
class ScanStatus:
    running: bool = False
    phase: str = "idle"            # scan | extract | flush | idle
    current_root: str = ""
    pending: int = 0
    total: int = 0                 # 今回の抽出対象総数 (done + pending の初期値)
    done: int = 0
    added: int = 0
    updated: int = 0
    moved: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    started_at: float = 0.0        # 抽出フェーズ開始時刻 (time.time())
    cancel_requested: bool = False
    cancelled: bool = False        # 直近スキャンが停止要求で中断されたか

    def eta_seconds(self) -> float | None:
        """残り推定秒。抽出を1件以上終えていれば実測レートから算出。"""
        if self.phase != "extract" or self.done == 0 or self.started_at == 0:
            return None
        elapsed = time.time() - self.started_at
        rate = self.done / elapsed  # 件/秒
        if rate <= 0:
            return None
        return self.pending / rate

    def rate_per_min(self) -> float | None:
        if self.started_at == 0 or self.done == 0:
            return None
        elapsed = time.time() - self.started_at
        return round(self.done / elapsed * 60, 1) if elapsed > 0 else None

    def snapshot(self) -> dict:
        return {
            "running": self.running, "phase": self.phase,
            "current_root": self.current_root,
            "queue": {"pending": self.pending},
            "total": self.total,
            "done_total": self.done,
            "eta_seconds": self.eta_seconds(),
            "rate_per_min": self.rate_per_min(),
            "cancel_requested": self.cancel_requested,
            "cancelled": self.cancelled,
            "counts": {"added": self.added, "updated": self.updated,
                       "moved": self.moved, "deleted": self.deleted,
                       "skipped": self.skipped},
            "errors_recent": self.errors[-5:],
        }


STATUS = ScanStatus()
_scan_lock = threading.Lock()


# ------------------------------------------------------------ 差分判定 -----

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


def diff_scan(db: sqlite3.Connection, root_id: int, root: Path,
              exts: set[str], recursive: bool = True) -> list[tuple[int, Path]]:
    """走査して差分のみ抽出ジョブ化。返り値: [(photo_id, 絶対パス)]

    recursive=False のときはルート直下のファイルのみを対象にする。
    このとき tombstone 走査もサブフォルダ配下の写真を消さないよう、
    known をルート直下のパスに限定する。
    """
    # tombstone (deleted=1) も含めて引く。同じパスにファイルが戻ってきたとき
    # INSERT が UNIQUE(root_id, path) に当たって復活できなくなるのを防ぐ
    known = {
        r["path"]: dict(r)
        for r in db.execute(
            "SELECT id, path, size, mtime, deleted FROM photos WHERE root_id=?",
            (root_id,),
        )
        # 非再帰スキャンではサブフォルダの既存写真を tombstone 対象から除外
        if recursive or (os.sep not in r["path"] and "/" not in r["path"])
    }
    jobs: list[tuple[int, Path]] = []
    seen: set[str] = set()
    moved_ids: set[int] = set()  # このパスで移動した id (末尾の tombstone 走査から守る)

    walker = root.rglob("*") if recursive else root.glob("*")
    for p in sorted(walker):
        if not p.is_file() or p.suffix.lower().lstrip(".") not in exts:
            continue
        rel = str(p.relative_to(root))
        seen.add(rel)
        try:
            st = p.stat()
        except OSError as e:
            STATUS.errors.append(f"{p}: {e}")
            continue
        rec = known.get(rel)

        if (rec and not rec["deleted"]
                and rec["size"] == st.st_size and abs(rec["mtime"] - st.st_mtime) < 1):
            STATUS.skipped += 1
            continue

        h = fast_hash(p, st.st_size)
        # tombstone (deleted=1) も候補に含める。複数ルートを1回の scan_all で走査する際、
        # 移動元ルートが先に処理されると移動先を見る前にファイルが tombstone 済みになり
        # (deleted=0 のみだと) 移動として検出できず新規行になってしまうため。
        # 生きている行があればそちらを優先、無ければ最近 tombstone された行を使う
        dup = db.execute(
            "SELECT id, root_id, path FROM photos WHERE xxhash=? "
            "ORDER BY deleted ASC, updated_at DESC LIMIT 1", (h,)
        ).fetchone()

        dup_root = db.execute(
            "SELECT path FROM roots WHERE id=?", (dup["root_id"],)
        ).fetchone() if dup else None
        if (dup and dup_root and dup["id"] != (rec or {}).get("id")
                and not (Path(dup_root["path"]) / dup["path"]).exists()):
            # 移動/リネーム。移動先パスに既存レコードがあれば（tombstone か、上書き
            # リネームで内容ごと置き換えられた生きているレコードかを問わず）先に完全除去。
            # 生きている rec を残したまま dup の path を書き換えると
            # UNIQUE(root_id, path) に衝突してスキャン全体が失敗する
            if rec:
                tid = rec["id"]
                db.execute("DELETE FROM photos_fts WHERE rowid=?", (tid,))
                db.execute("DELETE FROM photo_rtree WHERE photo_id=?", (tid,))
                db.execute("DELETE FROM faiss_pending WHERE photo_id=?", (tid,))
                db.execute("DELETE FROM photos WHERE id=?", (tid,))
            old_path = dup["path"]
            db.execute(
                "UPDATE photos SET root_id=?, path=?, mtime=?, deleted=0, "
                "updated_at=datetime('now') WHERE id=?",
                (root_id, rel, st.st_mtime, dup["id"]),
            )
            # tombstone 経由の復活 (移動元が先に処理され一度 deleted=1 になったケース) では
            # tombstone 処理で photos_fts/photo_rtree が既に削除済み。exif/geo/tags は
            # 消していないので DB 上の現状態だけで安価に復元できる (再抽出・再ハッシュ不要)。
            # 同一パスでの移動 (tombstone を経ない) では元々 fts/rtree は消えていないため
            # 冪等な再構築になるだけで無害
            from .fts import update_fts
            update_fts(db, dup["id"])
            erow = db.execute(
                "SELECT gps_lat, gps_lon FROM exif WHERE photo_id=?", (dup["id"],)
            ).fetchone()
            if erow and erow["gps_lat"] is not None:
                db.execute(
                    "INSERT OR REPLACE INTO photo_rtree VALUES (?,?,?,?,?)",
                    (dup["id"], erow["gps_lat"], erow["gps_lat"],
                     erow["gps_lon"], erow["gps_lon"]),
                )
            # 旧パスの known エントリを除去しないと、末尾の tombstone 走査が
            # 「消えたパス」として同じ id を deleted=1 に戻してしまう
            if (dup["root_id"] == root_id
                    and (known.get(old_path) or {}).get("id") == dup["id"]):
                known.pop(old_path, None)
            moved_ids.add(dup["id"])
            STATUS.moved += 1
        elif rec:
            # 内容変更 or tombstone 復活: レコードを現状に合わせて再抽出キューへ
            db.execute(
                "UPDATE photos SET size=?, mtime=?, xxhash=?, deleted=0, "
                "index_state='pending', updated_at=datetime('now') WHERE id=?",
                (st.st_size, st.st_mtime, h, rec["id"]),
            )
            jobs.append((rec["id"], p))
            STATUS.updated += 1
        else:
            cur = db.execute(
                "INSERT INTO photos (root_id, path, size, mtime, xxhash, ext) "
                "VALUES (?,?,?,?,?,?)",
                (root_id, rel, st.st_size, st.st_mtime, h,
                 p.suffix.lower().lstrip(".")),
            )
            jobs.append((cur.lastrowid, p))
            STATUS.added += 1

    for rel in known.keys() - seen:
        if known[rel]["deleted"] or known[rel]["id"] in moved_ids:
            continue  # 既に tombstone 済み / このパスで移動済み
        tid = known[rel]["id"]
        db.execute("UPDATE photos SET deleted=1 WHERE id=?", (tid,))
        # FTS/rtree は deleted=0 を前提に絞り込んでいないため、残すと削除済み写真が
        # 検索候補の枠を消費してしまう (大規模ライブラリでの recall 低下)
        db.execute("DELETE FROM photos_fts WHERE rowid=?", (tid,))
        db.execute("DELETE FROM photo_rtree WHERE photo_id=?", (tid,))
        db.execute(
            "INSERT OR REPLACE INTO faiss_pending (photo_id, op) VALUES (?, 'remove')",
            (tid,),
        )
        STATUS.deleted += 1

    db.commit()
    return jobs


# ------------------------------------------------------ 抽出パイプライン ---

def _open_working_image(path: Path) -> Image.Image:
    """JPEG/PNG/HEIC/RAW を問わず、以降のML/サムネ処理で共有するRGB画像を
    1枚返す。RAW (raw_utils.RAW_EXTS) は埋め込みプレビュー抽出、それ以外は
    通常のPillowデコード。呼び出し側 (extract_one/get_or_make_preview) の
    2箇所で向き/色変換ロジックが将来ズレないよう、ここに一本化する。
    """
    ext = path.suffix.lower().lstrip(".")
    if raw_utils.is_raw(ext):
        return raw_utils.load_raw_preview(path)
    # with で開き、画素データを読み切ってから閉じる。開いたまま関数末尾まで
    # ぶら下げると (数万枚規模のスキャンで) プロセスのFD上限に達しうる
    # (exif_transpose/convert は変換不要時に元の遅延読込オブジェクトを
    # そのまま返すことがあるため、load() を明示しないと with を抜けた時点で
    # img 自体が閉じたファイルを指したままになる)
    with Image.open(path) as im:
        img = ImageOps.exif_transpose(im)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.load()
        return img


def extract_one(db: sqlite3.Connection, photo_id: int, path: Path,
                data_dir: Path) -> None:
    """1枚をフル解析して DB 確定。M5: + 野鳥種名推定。"""
    from .bird import classifier as bird_clf
    from .exif_utils import extract_exif
    from .detector import detector
    from .fts import update_fts
    from .geo import reverse_geocode
    from .ocr import engine as ocr_engine
    from . import ml

    img = _open_working_image(path)

    e = extract_exif(path)
    taken_at = e.get("taken_at")
    if not taken_at:  # EXIF に日時が無ければ mtime
        from datetime import datetime
        taken_at = datetime.fromtimestamp(path.stat().st_mtime).isoformat()

    row = db.execute("SELECT xxhash FROM photos WHERE id=?", (photo_id,)).fetchone()
    _save_thumb(img, row["xxhash"], data_dir)

    # 作業画像 (長辺1024px) を全 ML タスクで共有 (docs/design.md §2)
    work = img.copy()
    work.thumbnail((1024, 1024))

    phash = ml.phash64(work)
    if ml.runtime.available:
        vec = ml.runtime.siglip_image(work)
        db.execute(
            "INSERT OR REPLACE INTO faiss_pending (photo_id, op, vector) "
            "VALUES (?, 'add', ?)",
            (photo_id, vec.astype("float16").tobytes()),
        )

    # 物体検出 → detections + 自動タグ (日本語ラベル)。再抽出時は旧結果を差し替え
    if detector.available:
        db.execute("DELETE FROM detections WHERE photo_id=?", (photo_id,))  # bird_ids は CASCADE
        db.execute(  # 未確定(verified=0)の自動タグのみ消す。'color' を残すと古い色タグが
            # FTS に残留するため対象に含める。verified!=0 (確定/否認済み) はユーザ操作の
            # 結果なので再抽出のたびに消えては困る — ここで除外し、_upsert_auto_tag 側の
            # ON CONFLICT ... WHERE verified=0 と合わせて再抽出後も維持されるようにする
            "DELETE FROM photo_tags WHERE photo_id=? AND source IN ('yolo','bird','color') "
            "AND verified = 0",
            (photo_id,))
        best: dict[str, float] = {}      # YOLO クラス名 → 最高信頼度
        species: dict[str, float] = {}   # 鳥種和名 → 最高信頼度
        colors: dict[str, float] = {}    # 色タグ (「青い鳥」等) → 最高類似度
        for d in detector.detect(work):
            cur = db.execute(
                "INSERT INTO detections (photo_id, label, conf, bbox) VALUES (?,?,?,?)",
                (photo_id, d["label"], d["conf"],
                 ",".join(f"{v:.4f}" for v in d["bbox"])))
            best[d["label_ja"]] = max(best.get(d["label_ja"], 0.0), d["conf"])

            # 野鳥の種名推定 + 色タグ (どちらも SigLIP ゼロショット、埋め込みは1回だけ計算)。
            # 種名の自動確定は自信のあるときだけ。それ以外は topk を UI の候補として残す
            if d["label"] == "bird" and bird_clf.available:
                from .colors import color_tags_vec
                crop_vec = ml.runtime.siglip_image(
                    _crop(work, d["bbox"], BIRD_CROP_PAD))
                sp = bird_clf.classify_vec(crop_vec)
                if sp:
                    db.execute(
                        "INSERT OR REPLACE INTO bird_ids "
                        "(detection_id, species_ja, species_sci, conf, topk_json, confirmed) "
                        "VALUES (?,?,?,?,?,0)",
                        (cur.lastrowid, sp["species_ja"] or "", sp["species_sci"],
                         sp["conf"], sp["topk_json"]))
                    if sp["species_ja"]:
                        species[sp["species_ja"]] = max(
                            species.get(sp["species_ja"], 0.0), sp["conf"])
                for cname, csim in color_tags_vec(crop_vec):
                    colors[cname] = max(colors.get(cname, 0.0), csim)
        for name, conf in best.items():
            _upsert_auto_tag(db, photo_id, name, conf, kind="auto", source="yolo")
        for name, conf in species.items():
            _upsert_auto_tag(db, photo_id, name, conf, kind="species", source="bird")
        for name, conf in colors.items():
            _upsert_auto_tag(db, photo_id, name, conf, kind="auto", source="color")

    # OCR (日本語): 写真内テキスト → ocr_texts (FTS 経由で検索可能に)
    if ocr_engine.available:
        db.execute("DELETE FROM ocr_texts WHERE photo_id=?", (photo_id,))
        for o in ocr_engine.run(work):
            db.execute(
                "INSERT INTO ocr_texts (photo_id, text, conf, bbox) VALUES (?,?,?,?)",
                (photo_id, o["text"], o["conf"], o["bbox"]))
    # --- M5 以降のフック地点: bird / landmark ---

    db.execute(
        "UPDATE photos SET width=?, height=?, taken_at=?, phash=?, ml_version=?, "
        "index_state='complete', updated_at=datetime('now') WHERE id=?",
        (img.width, img.height, taken_at, phash, ML_VERSION, photo_id),
    )
    db.execute(
        """INSERT OR REPLACE INTO exif
           (photo_id, camera_make, camera_model, lens_model, focal_length_mm,
            f_number, shutter_speed, iso, gps_lat, gps_lon, gps_img_direction, raw_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (photo_id, e.get("make"), e.get("model"), e.get("lens"), e.get("focal"),
         e.get("f"), e.get("ss"), e.get("iso"), e.get("lat"), e.get("lon"),
         e.get("direction"), json.dumps(e, ensure_ascii=False)),
    )
    if e.get("lat") is not None and e.get("lon") is not None:
        db.execute(
            "INSERT OR REPLACE INTO photo_rtree VALUES (?,?,?,?,?)",
            (photo_id, e["lat"], e["lat"], e["lon"], e["lon"]),
        )
        # 手動修正 (poi_source='manual') は上書きしない
        cur = db.execute("SELECT poi_source FROM geo WHERE photo_id=?",
                         (photo_id,)).fetchone()
        if not (cur and cur["poi_source"] == "manual"):
            g = reverse_geocode(e["lat"], e["lon"], e.get("direction"))
            if g:
                db.execute(
                    "INSERT OR REPLACE INTO geo "
                    "(photo_id, country, prefecture, city, poi_name, poi_conf, "
                    " poi_alt, poi_source) VALUES (?,?,?,?,?,?,?,'osm_nearby')",
                    (photo_id, g["country"], g["prefecture"], g["city"],
                     g["poi_name"], g["poi_conf"], g["poi_alt"]))
    else:
        # 以前は GPS があったが今回のEXIFには無い場合 (機材変更/内容差し替え等)、
        # 古い座標・場所情報を残さない。手動設定 (poi_source='manual') は保持する
        db.execute("DELETE FROM photo_rtree WHERE photo_id=?", (photo_id,))
        cur = db.execute("SELECT poi_source FROM geo WHERE photo_id=?",
                         (photo_id,)).fetchone()
        if not (cur and cur["poi_source"] == "manual"):
            db.execute("DELETE FROM geo WHERE photo_id=?", (photo_id,))
    update_fts(db, photo_id)
    db.commit()


def _upsert_auto_tag(db: sqlite3.Connection, photo_id: int, name: str, conf: float,
                     kind: str = "auto", source: str = "yolo") -> None:
    db.execute("INSERT OR IGNORE INTO tags (name, kind) VALUES (?, ?)", (name, kind))
    tag_id = db.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()["id"]
    # ユーザが確定/否認済み (verified != 0) のタグは上書きしない
    db.execute(
        """INSERT INTO photo_tags (photo_id, tag_id, conf, source, verified)
           VALUES (?,?,?,?,0)
           ON CONFLICT(photo_id, tag_id) DO UPDATE SET conf=excluded.conf
           WHERE photo_tags.verified = 0""",
        (photo_id, tag_id, conf, source))


def _crop(img: Image.Image, bbox: tuple[float, float, float, float],
          pad: float = 1.0) -> Image.Image:
    """0-1 正規化 bbox (x, y, w, h) を pad 倍に広げて切り出す。"""
    w, h = img.size
    x, y, bw, bh = bbox
    cx, cy = (x + bw / 2) * w, (y + bh / 2) * h
    pw, ph = bw * w * pad / 2, bh * h * pad / 2
    box = (max(0, int(cx - pw)), max(0, int(cy - ph)),
           min(w, int(cx + pw)), min(h, int(cy + ph)))
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return img
    return img.crop(box)


def _save_thumb(img: Image.Image, xxh: str, data_dir: Path) -> Path:
    out_dir = data_dir / "thumbs" / xxh[:2]
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{xxh}.webp"
    t = img.copy()
    t.thumbnail((THUMB_EDGE, THUMB_EDGE))
    t.save(out, "WEBP", quality=80)
    return out


def get_or_make_preview(db: sqlite3.Connection, photo_id: int,
                        data_dir: Path) -> Path | None:
    """1600px プレビュー。初回要求時に遅延生成。"""
    row = db.execute(
        """SELECT p.xxhash, p.path, r.path AS root_path
           FROM photos p JOIN roots r ON r.id = p.root_id
           WHERE p.id=? AND p.deleted=0""",
        (photo_id,),
    ).fetchone()
    if not row:
        return None
    out = data_dir / "previews" / row["xxhash"][:2] / f"{row['xxhash']}.webp"
    if out.exists():
        return out
    src = Path(row["root_path"]) / row["path"]
    if not src.exists():
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    img = _open_working_image(src)
    img.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE))
    img.save(out, "WEBP", quality=85)
    return out


# ------------------------------------------------------------ 実行入口 -----

def scan_all(db: sqlite3.Connection, data_dir: Path,
             root_id: int | None = None, vstore=None, wait: bool = False) -> dict:
    """全ルート（または指定ルート）をスキャン + 抽出。同時実行は 1 本に制限。

    wait=True は先行スキャンの完了を待ってから実行する
    (フォルダ追加直後のスキャンが黙って捨てられるのを防ぐ)。
    """
    if not _scan_lock.acquire(blocking=wait):
        return {"started": False, "reason": "scan already running"}
    try:
        STATUS.__init__()  # カウンタ + cancel フラグをリセット
        STATUS.running = True

        where = "enabled=1" + (" AND id=?" if root_id else "")
        args = (root_id,) if root_id else ()
        roots = db.execute(f"SELECT * FROM roots WHERE {where}", args).fetchall()

        all_jobs: list[tuple[int, Path]] = []
        for r in roots:
            if STATUS.cancel_requested:
                break
            STATUS.phase, STATUS.current_root = "scan", r["path"]
            exts = set(r["ext_filter"].lower().split(";"))
            root = Path(r["path"])
            if not root.exists():
                STATUS.errors.append(f"root not found: {root}")
                continue
            recursive = bool(r["recursive"]) if "recursive" in r.keys() else True
            all_jobs += diff_scan(db, r["id"], root, exts, recursive)

        # バックフィル: 旧 ML パイプラインの写真 + 失敗/中断 (error/pending) の再試行。
        # 特定 root を指定したときはその root 配下のみ (フォルダ別再スキャン用)
        queued = {pid for pid, _ in all_jobs}
        bf_where = "p.deleted=0 AND (p.ml_version < ? OR p.index_state IN ('pending','error'))"
        bf_args: tuple = (ML_VERSION,)
        if root_id:
            bf_where += " AND p.root_id=?"
            bf_args = (ML_VERSION, root_id)
        for r in db.execute(
            f"""SELECT p.id, p.path, r.path AS root_path FROM photos p
                JOIN roots r ON r.id=p.root_id WHERE {bf_where}""", bf_args):
            if r["id"] not in queued:
                all_jobs.append((r["id"], Path(r["root_path"]) / r["path"]))

        STATUS.phase, STATUS.pending = "extract", len(all_jobs)
        STATUS.total = len(all_jobs)
        STATUS.started_at = time.time()
        for photo_id, path in all_jobs:
            if STATUS.cancel_requested:
                # 未処理分は index_state='pending' のまま残し、次回スキャンで再開する
                # (レビュー対応で入れた pending/error 再キュー機構をそのまま利用)
                STATUS.cancelled = True
                break
            try:
                extract_one(db, photo_id, path, data_dir)
            except Exception as e:
                log.exception("extract failed: %s", path)
                STATUS.errors.append(f"{path.name}: {e}")
                # 途中まで進んだ変更 (旧タグ/検出の DELETE 等) を巻き戻す。
                # rollback しないと部分削除が commit されてしまう
                db.rollback()
                prev = db.execute(
                    "SELECT index_state FROM photos WHERE id=?", (photo_id,)).fetchone()
                if prev and prev["index_state"] == "complete":
                    # 既に検索可能だった写真 (ML_VERSION 更新後のバックフィル対象等)
                    # が一時的な失敗 (メモリ不足・破損ファイル等) で検索結果から
                    # 消えないよう complete のまま維持する。rollback で ml_version は
                    # 更新されていないため、次回スキャンのバックフィル条件
                    # (ml_version < ML_VERSION) に引っかかり自動的に再試行される。
                    # 一度も抽出に成功したことがない写真 (pending/error) のみ
                    # 今まで通り error にして次回リトライ対象として残す
                    log.warning("extract failed on already-complete photo (kept "
                                "complete for search visibility): %s", path)
                else:
                    db.execute(
                        "UPDATE photos SET index_state='error' WHERE id=?", (photo_id,))
                    db.commit()
            STATUS.done += 1
            STATUS.pending -= 1

        if vstore is not None:
            STATUS.phase = "flush"
            try:
                n = vstore.flush_pending(db)
                log.info("faiss flush: %d vectors", n)
            except Exception as e:
                # 例外を伝播させると STATUS.phase="flush" のまま return に到達せず、
                # running=False だけ finally で解除されて進捗表示が「保存中…」で
                # 固まって見える不具合になる。faiss_pending は flush_pending 内で
                # save() 成功後にしか DELETE しないため、行は消えず次回スキャンの
                # 冒頭で再度 flush が試みられる (中身は失われない)
                log.exception("faiss flush failed")
                STATUS.errors.append(f"faiss flush: {e}")

        STATUS.phase = "idle"
        return {"started": True, **STATUS.snapshot()}
    finally:
        STATUS.running = False
        _scan_lock.release()
