"""鳥の色タグ (M5 仕様変更): SigLIP ゼロショットで判定する。

「青い鳥」「茶色い鳥」のような複合タグを自動付与し、
自然な日本語クエリ (青い鳥) が FTS に直接ヒットするようにする。

ピクセル統計 (HSV ヒストグラム) を使わない理由:
  bbox 内の背景 (空・ボケ・水面) と鳥をヒューリスティックで分離できず、
  実写4枚の検証で 0/4 だった。SigLIP は「青い鳥という概念」への類似で判定する
  ため背景に頑健で、同じ検証で 3/4 (残り1枚はターコイズの緑/青僅差)。

付与ルール (実測較正):
  - top1 は sim >= MIN_SIM のとき常に付与
  - top2 は top1 との差が NEAR_MARGIN 以内のとき追加 (ターコイズ→緑+青、メジロ→緑+黄)
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading

import numpy as np
from PIL import Image

from .paths import data_dir

log = logging.getLogger("photofinder.colors")

CACHE_PATH = data_dir() / "color_bank.npz"

COLORS = [
    ("青い鳥", "blue bird"), ("赤い鳥", "red bird"),
    ("オレンジ色の鳥", "orange bird"), ("黄色い鳥", "yellow bird"),
    ("緑の鳥", "green bird"), ("紫の鳥", "purple bird"),
    ("ピンクの鳥", "pink bird"), ("茶色い鳥", "brown bird"),
    ("白い鳥", "white bird"), ("黒い鳥", "black bird"),
    ("灰色の鳥", "gray bird"),
]
TEMPLATES = ("{ja}の写真", "a photo of a {en}")

MIN_SIM = 0.06       # これ未満なら色タグを付けない
NEAR_MARGIN = 0.02   # top2 がこの差以内なら2色目として付与

_lock = threading.Lock()
_bank: np.ndarray | None = None


def _load_bank() -> np.ndarray:
    global _bank
    with _lock:
        if _bank is not None:
            return _bank
        from . import ml
        # ml.DIM を混ぜて埋め込みモデル変更時 (次元が変わる) もキャッシュを無効化する
        # (bird.py の _species_hash と同じ理由)
        h = hashlib.sha256(
            (json.dumps(COLORS, ensure_ascii=False) + str(TEMPLATES) + str(ml.DIM)).encode()
        ).hexdigest()[:16]
        if CACHE_PATH.exists():
            cached = np.load(CACHE_PATH)
            if str(cached.get("hash")) == h:
                _bank = cached["bank"]
                return _bank
        log.info("building color text bank…")
        vecs = []
        for ja, en in COLORS:
            embs = [ml.runtime.siglip_text(t.format(ja=ja, en=en)) for t in TEMPLATES]
            v = np.mean(embs, axis=0)
            vecs.append(v / (np.linalg.norm(v) or 1.0))
        _bank = np.stack(vecs).astype(np.float32)
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        np.savez(CACHE_PATH, bank=_bank, hash=h)
        return _bank


def color_tags_vec(crop_vec: np.ndarray) -> list[tuple[str, float]]:
    """SigLIP 画像埋め込み (bird crop) → [(色タグ名, 類似度)] 最大2件。"""
    bank = _load_bank()
    sims = bank @ crop_vec
    order = sims.argsort()[::-1]
    out: list[tuple[str, float]] = []
    if float(sims[order[0]]) >= MIN_SIM:
        out.append((COLORS[order[0]][0], round(float(sims[order[0]]), 4)))
        if (len(order) > 1
                and float(sims[order[0]] - sims[order[1]]) <= NEAR_MARGIN
                and float(sims[order[1]]) >= MIN_SIM):
            out.append((COLORS[order[1]][0], round(float(sims[order[1]]), 4)))
    return out


def bird_color_tags(crop: Image.Image) -> list[tuple[str, float]]:
    """画像から直接 (テスト用)。本番は color_tags_vec に埋め込みを渡して再利用する。"""
    from . import ml
    return color_tags_vec(ml.runtime.siglip_image(crop))
