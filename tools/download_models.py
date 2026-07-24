"""SigLIP2 多言語 ONNX モデル (int8 量子化) 等を models/ に取得する。

初回セットアップ時に1度だけ実行 (Dockerでは model-fetch ステージから、
docker-compose --profile setup run --rm model-fetch 経由で呼ばれる):
    python tools/download_models.py

取得元: onnx-community/siglip2-so400m-patch14-384-ONNX
(google/siglip2-so400m-patch14-384 の ONNX 変換版。WebLI 109言語で学習済み、
日本語含む。旧 pulsejet/siglip-base-patch16-256-multilingual-onnx [768d, 256px]
から精度優先で置き換え [1152d, 384px]。入出力テンソル名は同一だが次元と
解像度が変わるため、models/siglip 配下を丸ごと入れ替える必要がある
[ml.py の DIM/IMAGE_SIZE 参照]。計 ~1.1GB)

YOLOv8x (物体検出) だけは事情が異なる: 非公式 HuggingFace 再配布に頼らず、
Ultralytics 公式の .pt 重みから **その場で ONNX へエクスポート**する
(export_yolo() 参照)。`ultralytics` パッケージ (torch 等を含み重い) が
必要だが、これは変換作業だけに使う一時的なツールであり、requirements.txt
には加えていない。初回セットアップ時のみ:
    pip install ultralytics
    python tools/download_models.py
    pip uninstall ultralytics torch torchvision -y
    （変換後は不要。実行時は onnxruntime だけで動く。Dockerのmodel-fetchステージは
    この3手順をそのままRUNで実行する）
"""
from __future__ import annotations

import shutil
from pathlib import Path

from huggingface_hub import hf_hub_download

MODELS_ROOT = Path(__file__).parent.parent / "models"

# (repo, repo内ファイル, 保存先サブディレクトリ)
DOWNLOADS = [
    # SigLIP2 多言語 (M2: 自然言語/画像類似検索)
    ("onnx-community/siglip2-so400m-patch14-384-ONNX",
     "onnx/text_model_quantized.onnx", "siglip"),
    ("onnx-community/siglip2-so400m-patch14-384-ONNX",
     "onnx/vision_model_quantized.onnx", "siglip"),
    ("onnx-community/siglip2-so400m-patch14-384-ONNX", "tokenizer.json", "siglip"),
    ("onnx-community/siglip2-so400m-patch14-384-ONNX",
     "preprocessor_config.json", "siglip"),
    ("onnx-community/siglip2-so400m-patch14-384-ONNX", "config.json", "siglip"),
    # 日本語 OCR 認識モデル (M4)。検出モデルは rapidocr-onnxruntime 同梱のものを使用
    ("cycloneboy/japan_PP-OCRv3_rec_infer", "model.onnx", "ocr"),
    ("cycloneboy/japan_PP-OCRv3_rec_infer", "japan_dict.txt", "ocr"),
    # M5 の野鳥種名推定は SigLIP のゼロショット流用のため追加モデル不要
    # (専用分類器を使わない理由は photofinder/bird.py の docstring 参照)
    # YOLOv8m (M3: 物体検出 → 自動タグ) は export_yolo() で別途生成する
]

RENAME = {("cycloneboy/japan_PP-OCRv3_rec_infer", "model.onnx"): "japan_rec.onnx"}


def export_yolo() -> None:
    """Ultralytics 公式の yolov8x.pt から yolov8x.onnx をその場で生成する。

    非公式 HuggingFace 再配布 (旧 yolov8n はこれに依存していた) に頼らず、
    ライセンス上の出所が明確な一次情報から直接変換する。
    """
    out = MODELS_ROOT / "yolo" / "yolov8x.onnx"
    if out.exists():
        print("exists:", out.name)
        return
    try:
        from ultralytics import YOLO
    except ImportError:
        raise SystemExit(
            "yolov8x.onnx が無く、変換には ultralytics パッケージが必要です。\n"
            "  .venv\\Scripts\\python.exe -m pip install ultralytics\n"
            "  .venv\\Scripts\\python.exe tools\\download_models.py\n"
            "  .venv\\Scripts\\python.exe -m pip uninstall ultralytics torch torchvision -y\n"
            "  （変換後は不要）")
    out.parent.mkdir(parents=True, exist_ok=True)
    model = YOLO("yolov8x.pt")  # 初回は Ultralytics 公式 GitHub Release から自動取得
    exported = Path(model.export(format="onnx", imgsz=640, opset=12))
    shutil.move(str(exported), out)
    # ONNXだけあれば良い。.pt(PyTorch重み)は残さない。export は .pt と同じ場所に
    # 書き出すため、CWD ではなく exported の隣を見る (別ディレクトリから実行しても消える)
    exported.with_suffix(".pt").unlink(missing_ok=True)
    print(f"{out.name}: {out.stat().st_size // 1024 // 1024:,} MB")


def cleanup_obsolete() -> None:
    """旧版が生成した不要ファイルを掃除する (残っていると models ボリュームが肥大化する)。"""
    for name in ("yolov8n.onnx", "yolov8m.onnx"):  # v8x へ置き換え済み
        old = MODELS_ROOT / "yolo" / name
        if old.exists():
            old.unlink()
            print("removed obsolete:", old.name)


def main() -> None:
    for repo, f, sub in DOWNLOADS:
        dst = MODELS_ROOT / sub
        dst.mkdir(parents=True, exist_ok=True)
        out = dst / RENAME.get((repo, f), Path(f).name)
        if out.exists():
            print("exists:", out.name)
            continue
        p = hf_hub_download(repo_id=repo, filename=f)
        shutil.copy(p, out)
        print(f"{out.name}: {out.stat().st_size // 1024:,} KB")
    export_yolo()
    cleanup_obsolete()
    # "→" 等の非ASCII記号はコンソールのコードページ次第で UnicodeEncodeError に
    # なる (リダイレクト先が cp1252 の場合等) ため ASCII に留める
    print("done ->", MODELS_ROOT)


if __name__ == "__main__":
    main()
