"""書き出し (M4): 切り出し + 透かし + GPS/機材情報の除去。原本は変更しない。"""
from __future__ import annotations

import base64
import io
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from . import raw_utils

# 透かし画像 (data URL) のデコード後サイズガード。フロント側 (index.html
# WM_IMAGE_MAX_BYTES) にも同じ目安の上限があるが、APIを直接叩かれた場合の
# 保険としてサーバ側でも掛ける
WATERMARK_IMAGE_MAX_BYTES = 8 * 1024 * 1024
WATERMARK_IMAGE_MAX_PIXELS = 40_000_000  # ~40MP

# 透かし用フォント選択肢。id は UI の <select> と対応し、label は
# GET /api/export/options 経由でフロントに渡す (一覧の一元管理)。
# Dockerイメージに fonts-noto-cjk (+ fonts-noto-cjk-extra) をaptで導入する
# 前提のLinuxパス。旧app2/photofinderはWindows標準搭載フォント
# (C:\Windows\Fonts\...) を直接参照していたが、配布がDocker専用になり
# 存在しないパスを黙って踏んで _load_font() がビットマップ既定フォント
# (CJKグリフ無し、日本語が豆腐文字になる) にフォールバックしていた
NOTO_DIR = Path("/usr/share/fonts/opentype/noto")
# fonts-mplus (OFL-1.1、docs/third-party-notices.md参照) — Noto CJKと違い本物の
# 太字面を9ウェイト持つファミリーだが、既存パターンに合わせてRegular/Boldのみ収録
MPLUS_DIR = Path("/usr/share/fonts/opentype/mplus")
FONTS: dict[str, dict] = {
    "gothic": {"label": "ゴシック体（Noto Sans JP）",
               "path": str(NOTO_DIR / "NotoSansCJK-Regular.ttc")},
    "gothic-bold": {"label": "ゴシック体 太字（Noto Sans JP Bold）",
                    "path": str(NOTO_DIR / "NotoSansCJK-Bold.ttc")},
    "mincho": {"label": "明朝体（Noto Serif JP）",
               "path": str(NOTO_DIR / "NotoSerifCJK-Regular.ttc")},
    "mincho-bold": {"label": "明朝体 太字（Noto Serif JP Bold）",
                    "path": str(NOTO_DIR / "NotoSerifCJK-Bold.ttc")},
    "mplus": {"label": "M+ 1（丸みのあるゴシック）",
              "path": str(MPLUS_DIR / "Mplus1-Regular.otf")},
    "mplus-bold": {"label": "M+ 1 太字",
                   "path": str(MPLUS_DIR / "Mplus1-Bold.otf")},
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
    crop: dict | None = None,          # {x, y, w, h} 0-1 正規化
    watermark: dict | None = None,     # {text, position, opacity}
    strip_metadata: bool = True,       # True = EXIF 全除去 (GPS・シリアル等を確実に落とす)
    fmt: str = "jpeg",
    quality: int = 92,
    max_edge: int | None = 2048,
) -> tuple[bytes, str]:
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

    if watermark and (watermark.get("text") or watermark.get("image_data_url")):
        img = _draw_watermark(img, watermark)

    # EXIF: 既定は全除去 (GPS + シリアル等を確実に落とす)。
    # strip_metadata=False のときは元 EXIF をそのまま引き継ぐ (全部残る)
    save_exif = None
    if not strip_metadata and exif_bytes:
        save_exif = exif_bytes

    stem = re.sub(r"[^\w\-]", "_", src.stem)
    ext = {"jpeg": ".jpg", "png": ".png", "webp": ".webp"}[fmt]
    filename = f"{stem}_edit{ext}"

    kwargs: dict = {}
    if fmt in ("jpeg", "webp"):
        kwargs["quality"] = quality
    if save_exif:
        kwargs["exif"] = save_exif
    buf = io.BytesIO()
    img.save(buf, fmt.upper(), **kwargs)
    return buf.getvalue(), filename


def _load_watermark_image(data_url: str) -> Image.Image:
    """"data:image/...;base64,..." 形式の透かし画像をRGBAで読み込む。

    フロントはFileReaderでアルファチャンネル付きPNG/WebP等をそのままdata URL化して
    送ってくる (base64、JSON本文に載せる — multipartへの切り替えを避けるための選択)。
    """
    try:
        _, b64data = data_url.split(",", 1)
        raw = base64.b64decode(b64data)
    except (ValueError, base64.binascii.Error) as e:
        raise ValueError(f"invalid watermark image data: {e}") from e
    if len(raw) > WATERMARK_IMAGE_MAX_BYTES:
        raise ValueError(
            f"watermark image too large ({len(raw)} bytes, "
            f"max {WATERMARK_IMAGE_MAX_BYTES})")
    img = Image.open(io.BytesIO(raw))
    if img.width * img.height > WATERMARK_IMAGE_MAX_PIXELS:
        raise ValueError(f"watermark image resolution too large ({img.width}x{img.height})")
    img.load()
    return img.convert("RGBA")


def _draw_watermark(img: Image.Image, wm: dict) -> Image.Image:
    position = wm.get("position", "bottom-right")
    opacity = max(0.05, min(1.0, float(wm.get("opacity", 0.6))))
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    margin = max(10, img.height // 60)

    if wm.get("image_data_url"):
        wm_img = _load_watermark_image(wm["image_data_url"])
        # サイズは書き出し画像の幅に対する割合 (index.html の expWmImgSize と同じ指標
        # なのでプレビューとWYSIWYGになる)。アスペクト比は保持する
        size_pct = max(1.0, min(100.0, float(wm.get("image_size_pct", 20))))
        target_w = max(1, round(img.width * size_pct / 100))
        target_h = max(1, round(wm_img.height * target_w / wm_img.width))
        wm_img = wm_img.resize((target_w, target_h), Image.LANCZOS)
        if opacity < 1.0:  # 画像自体のアルファに、透過率スライダーをさらに掛け合わせる
            alpha = wm_img.split()[3].point(lambda a: round(a * opacity))
            wm_img.putalpha(alpha)
        tw, th = wm_img.size
        x = margin if "left" in position \
            else img.width - tw - margin if "right" in position \
            else (img.width - tw) / 2
        y = margin if "top" in position \
            else img.height - th - margin if "bottom" in position \
            else (img.height - th) / 2
        overlay.paste(wm_img, (round(x), round(y)), wm_img)
        return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    text = wm.get("text")
    if not text:
        return img
    draw = ImageDraw.Draw(overlay)
    # フォントサイズは書き出し画像の高さに対する割合 (index.html の expWmSize と
    # 同じ指標。既定2.5%は旧固定値 img.height//40 と一致させてある)
    size_pct = max(0.5, min(20.0, float(wm.get("size_pct", 2.5))))
    font = _load_font(max(14, round(img.height * size_pct / 100)), wm.get("font"))
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    tw, th = r - l, b - t

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
