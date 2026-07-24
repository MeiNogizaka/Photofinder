"""RAW画像 (CR2/ARW/ORF等) の埋め込みプレビュー抽出。

フル現像はしない — rawpy の extract_thumb() でカメラが生成した埋め込み
JPEG/ビットマッププレビューを取得し、既存の共有ワーキングイメージ
パイプライン (scanner.py の extract_one/get_or_make_preview) にそのまま
流し込む。SigLIP(384px)/YOLO(640px)の入力解像度を考えれば十分な解像度で
あり、デモザイクよりはるかに高速 (docs/design.md §2 の想定通り)。

埋め込みプレビューを持たない機種 (一部の旧カメラ/一部のDNG) のみ、
フォールバックとして half_size 現像を行う。
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

import rawpy
from PIL import Image, ImageOps

log = logging.getLogger("photofinder.raw_utils")

# scanner.py の Image.open() vs rawpy 分岐用のデフォルトセット。
# rawpy/libraw は実際にはこれよりずっと多くの形式に対応しているが、
# roots.ext_filter は自由入力欄なので、ユーザがここに無い拡張子を
# 追加登録しても is_raw() に含めれば同じ分岐ロジックがそのまま働く
# (ハードな許可リストではない)
RAW_EXTS = {"cr2", "cr3", "nef", "arw", "orf", "raf", "rw2", "dng", "pef"}


class RawDecodeError(Exception):
    """RAWファイルから埋め込みプレビュー/フォールバック現像のどちらも
    取得できなかった場合。scanner.py の既存の抽出失敗ハンドリング
    (index_state='error' での次回リトライ) にそのまま乗る。"""


def is_raw(ext: str) -> bool:
    return ext.lower().lstrip(".") in RAW_EXTS


def load_raw_preview(path: Path) -> Image.Image:
    """RAWファイルから埋め込みプレビューをRGB PIL Imageとして返す。"""
    try:
        with rawpy.imread(str(path)) as raw:
            try:
                thumb = raw.extract_thumb()
            except (rawpy.LibRawNoThumbnailError,
                    rawpy.LibRawUnsupportedThumbnailError):
                log.info("no embedded preview, falling back to half_size "
                          "demosaic: %s", path)
                rgb = raw.postprocess(use_camera_wb=True, half_size=True)
                return Image.fromarray(rgb).convert("RGB")

            if thumb.format == rawpy.ThumbFormat.JPEG:
                img = Image.open(io.BytesIO(thumb.data))
                # 埋め込みJPEG自体が向き情報を持つ機種があるため、通常の
                # デコード経路 (scanner.py) と同じくexif_transposeをかける
                img = ImageOps.exif_transpose(img)
            else:  # ThumbFormat.BITMAP — 稀 (一部の旧機種のみ)
                img = Image.fromarray(thumb.data)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.load()
            return img
    except RawDecodeError:
        raise
    except Exception as e:
        raise RawDecodeError(f"{path}: {e}") from e
