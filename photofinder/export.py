"""書き出し (M4): 切り出し + 透かし + GPS/機材情報の除去。原本は変更しない。"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import raw_utils

# 透かし用フォント選択肢。id は UI の <select> と対応し、label は
# GET /api/export/options 経由でフロントに渡す (一覧の一元管理)。
# Dockerイメージに fonts-noto-cjk (+ fonts-noto-cjk-extra) をaptで導入する
# 前提のLinuxパス。旧app2/photofinderはWindows標準搭載フォント
# (C:\Windows\Fonts\...) を直接参照していたが、配布がDocker専用になり
# 存在しないパスを黙って踏んで _load_font() がビットマップ既定フォント
# (CJKグリフ無し、日本語が豆腐文字になる) にフォールバックしていた
NOTO_DIR = Path("/usr/share/fonts/opentype/noto")
FONTS: dict[str, dict] = {
    "gothic": {"label": "ゴシック体（Noto Sans JP）",
               "path": str(NOTO_DIR / "NotoSansCJK-Regular.ttc")},
    "gothic-bold": {"label": "ゴシック体 太字（Noto Sans JP Bold）",
                    "path": str(NOTO_DIR / "NotoSansCJK-Bold.ttc")},
    "mincho": {"label": "明朝体（Noto Serif JP）",
               "path": str(NOTO_DIR / "NotoSerifCJK-Regular.ttc")},
    "mincho-bold": {"label": "明朝体 太字（Noto Serif JP Bold）",
                    "path": str(NOTO_DIR / "NotoSerifCJK-Bold.ttc")},
}
DEFAULT_FONT = "gothic"

# 3x3 配置 (中央は写真本体と重なるため対象外)
POSITIONS = (
    "top-left", "top", "top-right",
    "left", "right",
    "bottom-left", "bottom", "bottom-right",
)


def _load_font(size: int, font_key: str | None = None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    order = [font_key, *FONTS] if font_key in FONTS else list(FONTS)
    for key in dict.fromkeys(order):  # 重複除去しつつ順序維持
        try:
            # Noto Sans/Serif CJK の .ttc は地域別フェイスを1ファイルに同梱しており、
            # index省略時の既定 (0) が日本語フェイスを指す構成になっている
            # (Debian/Ubuntu fonts-noto-cjk パッケージでの配置)。要実機確認
            return ImageFont.truetype(FONTS[key]["path"], size)
        except OSError:
            continue
    return ImageFont.load_default()


def export_photo(
    src: Path,
    out_dir: Path,
    crop: dict | None = None,          # {x, y, w, h} 0-1 正規化
    watermark: dict | None = None,     # {text, position, opacity}
    strip_metadata: bool = True,       # True = EXIF 全除去 (GPS・シリアル等を確実に落とす)
    fmt: str = "jpeg",
    quality: int = 92,
    max_edge: int | None = 2048,
) -> Path:
    # with で開き、画素データを読み切ってから閉じる (開いたままだと大量書き出し時に
    # FD を消費し続ける。scanner.py の extract_one と同じ理由)。RAWはscanner.pyと
    # 同じく埋め込みプレビュー経由 (フル現像はしない) — 埋め込みJPEGなら自身の
    # EXIFを持つことがあるためexif_bytesも拾えるが、半解像度フォールバック時は
    # 情報が無いため空のままになる (他形式のEXIF欠如時と同じ扱い)
    ext = src.suffix.lower().lstrip(".")
    if raw_utils.is_raw(ext):
        img = raw_utils.load_raw_preview(src)
        exif_bytes = img.info.get("exif")
        img = img.convert("RGB")
    else:
        with Image.open(src) as im:
            exif_bytes = im.info.get("exif")
            img = ImageOps.exif_transpose(im).convert("RGB")
            img.load()

    if crop:
        w, h = img.size
        box = (round(crop["x"] * w), round(crop["y"] * h),
               round((crop["x"] + crop["w"]) * w), round((crop["y"] + crop["h"]) * h))
        box = (max(0, box[0]), max(0, box[1]), min(w, box[2]), min(h, box[3]))
        if box[2] - box[0] >= 16 and box[3] - box[1] >= 16:
            img = img.crop(box)

    if max_edge and max(img.size) > max_edge:
        img.thumbnail((max_edge, max_edge), Image.LANCZOS)

    if watermark and watermark.get("text"):
        img = _draw_watermark(img, watermark)

    # EXIF: 既定は全除去 (GPS + シリアル等を確実に落とす)。
    # strip_metadata=False のときは元 EXIF をそのまま引き継ぐ (全部残る)
    save_exif = None
    if not strip_metadata and exif_bytes:
        save_exif = exif_bytes

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w\-]", "_", src.stem)
    ext = {"jpeg": ".jpg", "png": ".png", "webp": ".webp"}[fmt]
    out = out_dir / f"{stem}_edit{ext}"
    n = 1
    while out.exists():
        out = out_dir / f"{stem}_edit_{n}{ext}"
        n += 1

    kwargs: dict = {}
    if fmt in ("jpeg", "webp"):
        kwargs["quality"] = quality
    if save_exif:
        kwargs["exif"] = save_exif
    img.save(out, fmt.upper(), **kwargs)
    return out


def _draw_watermark(img: Image.Image, wm: dict) -> Image.Image:
    text = wm["text"]
    position = wm.get("position", "bottom-right")
    opacity = max(0.05, min(1.0, float(wm.get("opacity", 0.6))))

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _load_font(max(14, img.height // 40), wm.get("font"))
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    tw, th = r - l, b - t
    margin = max(10, img.height // 60)

    # 横位置: left/right instruct 端寄せ、それ以外 (top/bottom 単独) は水平中央
    if "left" in position:
        x = margin
    elif "right" in position:
        x = img.width - tw - margin
    else:
        x = (img.width - tw) / 2
    # 縦位置: top/bottom は端寄せ、それ以外 (left/right 単独) は垂直中央
    if "top" in position:
        y = margin
    elif "bottom" in position:
        y = img.height - th - margin - t
    else:
        y = (img.height - th) / 2 - t

    alpha = round(255 * opacity)
    draw.text((x + 1, y + 1), text, font=font, fill=(0, 0, 0, alpha // 2))  # 影
    draw.text((x, y), text, font=font, fill=(255, 255, 255, alpha))
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
