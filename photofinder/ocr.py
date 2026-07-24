"""OCR (M4): RapidOCR (PP-OCR ONNX) + 日本語認識モデル。

検出は RapidOCR 同梱の ch_PP-OCRv4 det (言語非依存)、
認識は japan_PP-OCRv3 (models/ocr/japan_rec.onnx + japan_dict.txt)。
"""
from __future__ import annotations

import logging
import threading

import numpy as np
from PIL import Image

from .paths import app_root

log = logging.getLogger("photofinder.ocr")

MODELS_DIR = app_root() / "models" / "ocr"
REC_MODEL = MODELS_DIR / "japan_rec.onnx"
REC_DICT = MODELS_DIR / "japan_dict.txt"
MIN_CONF = 0.60          # 低信頼の誤読はノイズになるため保存しない
MAX_TEXT_LEN = 80


class OCREngine:
    def __init__(self):
        self._lock = threading.Lock()
        self._engine = None
        self._failed = False

    @property
    def available(self) -> bool:
        if self._failed:
            return False
        try:
            import rapidocr_onnxruntime  # noqa: F401
        except ImportError:
            return False
        return REC_MODEL.exists() and REC_DICT.exists()

    def _load(self):
        with self._lock:
            if self._engine is None:
                from rapidocr_onnxruntime import RapidOCR
                self._engine = RapidOCR(
                    rec_model_path=str(REC_MODEL),
                    rec_keys_path=str(REC_DICT),
                )
        return self._engine

    def run(self, img: Image.Image) -> list[dict]:
        """[{text, conf, bbox: 'x,y,w,h' 0-1正規化}]"""
        try:
            engine = self._load()
        except Exception:
            # モデル読み込み自体の失敗は環境要因なので以後スキップ (available=False)
            log.exception("ocr engine load failed")
            self._failed = True
            return []
        try:
            result, _ = engine(np.asarray(img.convert("RGB")))
        except Exception:
            # 1枚の不良/特殊画像での推論失敗は全体を止めない。その1枚だけスキップ
            log.exception("ocr inference failed for this image")
            return []
        if not result:
            return []
        w, h = img.size
        out = []
        for box, text, score in result:
            if float(score) < MIN_CONF or len(text.strip()) < 2:
                continue  # 1文字はほぼ誤検出ノイズ (「の」「～」等) のため捨てる
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            out.append({
                "text": text.strip()[:MAX_TEXT_LEN],
                "conf": round(float(score), 4),
                "bbox": f"{min(xs)/w:.4f},{min(ys)/h:.4f},"
                        f"{(max(xs)-min(xs))/w:.4f},{(max(ys)-min(ys))/h:.4f}",
            })
        return out


engine = OCREngine()
