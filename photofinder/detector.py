"""物体検出 (M3): YOLOv8x ONNX。

入力 640x640 レターボックス、出力 (1, 84, 8400) = box(cxcywh) + COCO 80クラス。
NMS は numpy 実装。返り値の bbox は元画像に対する 0-1 正規化 (x, y, w, h)。

v8以降 nano → medium → x と精度優先で変更 (docs/design.md 差分参照)。出力形状は
モデルサイズによらず同じ (84, 8400) のため decode/NMS ロジックは変更不要。
GPU (CUDAExecutionProvider) 前提でのサイズ選定 — CPU実行だと x は m の2倍以上遅い。
"""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
from PIL import Image

from .ml import ORT_PROVIDERS, session_provider
from .paths import app_root

MODEL_PATH = app_root() / "models" / "yolo" / "yolov8x.onnx"
INPUT_SIZE = 640
CONF_THRESHOLD = 0.35
IOU_THRESHOLD = 0.45

COCO_JA = [
    "人", "自転車", "車", "バイク", "飛行機", "バス", "電車", "トラック", "船",
    "信号機", "消火栓", "一時停止標識", "パーキングメーター", "ベンチ", "鳥", "猫",
    "犬", "馬", "羊", "牛", "象", "熊", "シマウマ", "キリン", "リュック", "傘",
    "ハンドバッグ", "ネクタイ", "スーツケース", "フリスビー", "スキー", "スノーボード",
    "ボール", "凧", "バット", "グローブ", "スケートボード", "サーフボード",
    "テニスラケット", "ボトル", "ワイングラス", "カップ", "フォーク", "ナイフ",
    "スプーン", "ボウル", "バナナ", "りんご", "サンドイッチ", "オレンジ",
    "ブロッコリー", "にんじん", "ホットドッグ", "ピザ", "ドーナツ", "ケーキ",
    "椅子", "ソファ", "観葉植物", "ベッド", "テーブル", "トイレ", "テレビ",
    "ノートPC", "マウス", "リモコン", "キーボード", "スマートフォン", "電子レンジ",
    "オーブン", "トースター", "シンク", "冷蔵庫", "本", "時計", "花瓶", "はさみ",
    "ぬいぐるみ", "ドライヤー", "歯ブラシ",
]
COCO_EN = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]


class Detector:
    def __init__(self, model_path: Path = MODEL_PATH):
        self.model_path = model_path
        self._lock = threading.Lock()
        self._session = None

    @property
    def available(self) -> bool:
        return self.model_path.exists()

    @property
    def active_provider(self) -> str | None:
        return session_provider(self._session)

    def _load(self):
        with self._lock:
            if self._session is None:
                import onnxruntime as ort
                self._session = ort.InferenceSession(
                    str(self.model_path), providers=ORT_PROVIDERS)
        return self._session

    def detect(self, img: Image.Image) -> list[dict]:
        """[{label, label_ja, conf, bbox: (x, y, w, h) 0-1正規化}]"""
        session = self._load()
        w0, h0 = img.size

        # レターボックス: アスペクト比保持で 640x640 に収める
        scale = min(INPUT_SIZE / w0, INPUT_SIZE / h0)
        nw, nh = round(w0 * scale), round(h0 * scale)
        pad_x, pad_y = (INPUT_SIZE - nw) / 2, (INPUT_SIZE - nh) / 2
        canvas = Image.new("RGB", (INPUT_SIZE, INPUT_SIZE), (114, 114, 114))
        canvas.paste(img.convert("RGB").resize((nw, nh), Image.BILINEAR),
                     (int(pad_x), int(pad_y)))
        x = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0

        out = session.run(None, {"images": x})[0][0]        # (84, 8400)
        boxes, scores, class_ids = _decode(out)
        # エクスポートによって box はレターボックス内の 0-1 正規化 or ピクセル座標。
        # 全座標が 1.5 以下なら正規化とみなし、640 スケールへ引き上げる
        if len(boxes) and boxes.max() <= 1.5:
            boxes = boxes * INPUT_SIZE
        keep = _nms(boxes, scores, IOU_THRESHOLD)

        results = []
        for i in keep:
            cx, cy, bw, bh = boxes[i]
            # レターボックス座標 → 元画像の 0-1 正規化
            bx = (cx - bw / 2 - pad_x) / scale / w0
            by = (cy - bh / 2 - pad_y) / scale / h0
            nw_, nh_ = bw / scale / w0, bh / scale / h0
            results.append({
                "label": COCO_EN[class_ids[i]],
                "label_ja": COCO_JA[class_ids[i]],
                "conf": round(float(scores[i]), 4),
                "bbox": (round(float(max(0.0, bx)), 4), round(float(max(0.0, by)), 4),
                         round(float(min(1.0, nw_)), 4),
                         round(float(min(1.0, nh_)), 4)),
            })
        return results


def _decode(out: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(84, 8400) → conf しきい値を超えた (boxes cxcywh, scores, class_ids)"""
    cls_scores = out[4:, :]                                  # (80, 8400)
    class_ids = cls_scores.argmax(axis=0)
    scores = cls_scores.max(axis=0)
    mask = scores >= CONF_THRESHOLD
    return out[:4, mask].T, scores[mask], class_ids[mask]


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thr: float) -> list[int]:
    """クラス非依存 NMS (cxcywh)。スコア降順の残存 index を返す。"""
    if len(boxes) == 0:
        return []
    x1 = boxes[:, 0] - boxes[:, 2] / 2
    y1 = boxes[:, 1] - boxes[:, 3] / 2
    x2 = boxes[:, 0] + boxes[:, 2] / 2
    y2 = boxes[:, 1] + boxes[:, 3] / 2
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= iou_thr]
    return keep


detector = Detector()
