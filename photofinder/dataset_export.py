"""人手タグ付け済み写真を JSONL + 画像zipとしてエクスポート (教師/評価データセット化)。

既存の確定/否認タグ機構をそのまま「正解データ」として使う新機能。

正解データの定義: photo_tags.verified != 0 の行のみ (0=未レビューは対象外)。
source='user' (手動追加、常にverified=1) と、source IN ('yolo','bird','color')
のうち人間が✓/✗を押した行 (verified=1/-1) の両方が対象になる。verified=-1
(人手による明示的な否認) はデフォルトで陰性例として含める — MIN_SIM/MIN_MARGIN
等の閾値再較正に有用な「モデルの提案を人間が見て却下した」例のため。

既知の注意点: bird_ids.confirmed は photo_tags.verified とは別カラムで、
既存の✓/✗UI (main.py の verify_tag()) からは一切更新されない (常に0のまま)。
種名詳細のゲートは bird_ids.confirmed ではなく、対応する photo_tags.verified
(種名タグ名で突き合わせ) を使う。
"""
from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

from . import scanner


def _tagged_photo_ids(db: sqlite3.Connection) -> list[int]:
    rows = db.execute(
        """SELECT DISTINCT p.id FROM photos p
           JOIN photo_tags pt ON pt.photo_id = p.id
           WHERE p.deleted=0 AND p.index_state='complete' AND pt.verified != 0
           ORDER BY p.id"""
    ).fetchall()
    return [r["id"] for r in rows]


def _photo_row(db: sqlite3.Connection, photo_id: int) -> dict:
    return dict(db.execute(
        """SELECT p.id, p.xxhash, p.width, p.height, p.taken_at, e.camera_model
           FROM photos p LEFT JOIN exif e ON e.photo_id = p.id
           WHERE p.id=?""", (photo_id,)).fetchone())


def _tag_rows(db: sqlite3.Connection, photo_id: int) -> list[sqlite3.Row]:
    return db.execute(
        """SELECT pt.verified, pt.conf, pt.source, t.name, t.kind
           FROM photo_tags pt JOIN tags t ON t.id = pt.tag_id
           WHERE pt.photo_id=? AND pt.verified != 0""", (photo_id,)).fetchall()


def _species_detail(db: sqlite3.Connection, photo_id: int,
                    species_verified: dict[str, int]) -> list[dict]:
    """detections+bird_ids を photo_tags.verified (種名タグ名で突き合わせ) でゲートする。
    bird_ids.confirmed は既存UIから一切更新されない別カラムのため使わない。"""
    rows = db.execute(
        """SELECT d.bbox, d.conf AS yolo_conf, b.species_ja, b.species_sci,
                  b.conf AS species_conf, b.topk_json
           FROM detections d JOIN bird_ids b ON b.detection_id = d.id
           WHERE d.photo_id=? AND b.species_ja != ''""", (photo_id,)).fetchall()
    out = []
    for r in rows:
        v = species_verified.get(r["species_ja"])
        if not v:
            continue  # 未レビュー、またはこの種名タグが無い (低確信で候補提示のみ)
        out.append({
            "bbox": r["bbox"], "yolo_conf": r["yolo_conf"],
            "species_ja": r["species_ja"], "species_sci": r["species_sci"],
            "species_conf": r["species_conf"],
            "topk": json.loads(r["topk_json"]) if r["topk_json"] else None,
            "verified": v,
        })
    return out


def build_dataset_zip(db: sqlite3.Connection, data_dir: Path, out_path: Path,
                      include_negatives: bool = True,
                      include_bird_detail: bool = True) -> dict:
    """人手タグ付け済み写真をzipに書き出す。返り値は件数サマリ。

    画像は既存の1600pxプレビューWebP (scanner.get_or_make_preview、無ければ
    その場で生成) を使う。オリジナルではない — SigLIP(384px)/YOLO(640px)の
    入力解像度を大きく上回り十分な上、既存の書き出し時GPS/EXIF除去方針とも
    一致し、RAW対応後のオリジナル肥大化も避けられる。
    """
    photo_ids = _tagged_photo_ids(db)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_positive = n_negative = 0
    lines: list[str] = []

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for pid in photo_ids:
            tag_rows = _tag_rows(db, pid)
            positive = [{"name": r["name"], "kind": r["kind"], "source": r["source"],
                        "conf": r["conf"]} for r in tag_rows if r["verified"] == 1]
            negative = [{"name": r["name"], "kind": r["kind"], "source": r["source"],
                        "conf": r["conf"]} for r in tag_rows if r["verified"] == -1]
            if not positive and not (include_negatives and negative):
                continue  # 陰性除外設定時、陰性しか無い写真はスキップ

            preview = scanner.get_or_make_preview(db, pid, data_dir)
            if preview is None:
                continue  # 原本が既に見つからない (削除/移動済み等)

            photo = _photo_row(db, pid)
            image_path = f"images/{photo['xxhash']}.webp"
            zf.write(preview, image_path)

            row = {
                "photo_id": pid, "xxhash": photo["xxhash"], "image_path": image_path,
                "width": photo["width"], "height": photo["height"],
                "taken_at": photo["taken_at"], "camera_model": photo["camera_model"],
                "positive_tags": positive,
            }
            n_positive += len(positive)
            if include_negatives:
                row["negative_tags"] = negative
                n_negative += len(negative)
            if include_bird_detail:
                species_verified = {r["name"]: r["verified"] for r in tag_rows
                                    if r["kind"] == "species"}
                detail = _species_detail(db, pid, species_verified)
                if detail:
                    row["species_detail"] = detail
            lines.append(json.dumps(row, ensure_ascii=False))

        zf.writestr("dataset.jsonl", "\n".join(lines) + ("\n" if lines else ""))

    return {"photos": len(lines), "positive_tags": n_positive, "negative_tags": n_negative}
