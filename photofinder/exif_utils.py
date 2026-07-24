"""EXIF 抽出。piexif ベース、日時/GPS/カメラ情報を正規化して返す。

RAW (CR2/ARW/ORF等) は piexif がJPEG/TIFFコンテナ前提のパースしかできず
黙って空を返すため、is_raw なファイルで piexif が空だった場合のみ
exifread ベースのフォールバックを試す (raw_utils.RAW_EXTS 参照)。
CR3はISO-BMFF (QuickTime系) コンテナのため、rawpy/librawで画像デコード
自体はできてもexifreadのTIFF前提パースではEXIFが空になりうる — 既知の
制約として許容し、値が取れない場合は他形式同様に空dictを返すのみとする。
"""
from __future__ import annotations

import json
from datetime import datetime
from fractions import Fraction
from pathlib import Path

import exifread
import piexif

from .raw_utils import is_raw


def _rational(v) -> float | None:
    try:
        num, den = v
        return num / den if den else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _dms_to_deg(dms, ref: bytes) -> float | None:
    try:
        d = _rational(dms[0]) or 0
        m = _rational(dms[1]) or 0
        s = _rational(dms[2]) or 0
        deg = d + m / 60 + s / 3600
        if ref in (b"S", b"W"):
            deg = -deg
        return round(deg, 7)
    except (TypeError, IndexError):
        return None


def _decode(v) -> str | None:
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace").strip("\x00 ").strip() or None
    return str(v) if v else None


def _format_shutter(r: float | None) -> str | None:
    if r is None or r <= 0:
        return None
    if r >= 1:
        return f"{r:g}"
    return f"1/{round(1 / r)}"


def _shutter(v) -> str | None:
    return _format_shutter(_rational(v))


def _exifread_ratio(v) -> float | None:
    """exifread の Ratio (または int) 1個を float に変換。"""
    try:
        if hasattr(v, "num"):
            return v.num / v.den if v.den else None
        return float(v)
    except (TypeError, ZeroDivisionError, AttributeError):
        return None


def _exifread_scalar(tag) -> float | None:
    """exifread の IfdTag (単一数値) を float に変換。"""
    if tag is None:
        return None
    try:
        return _exifread_ratio(tag.values[0])
    except (TypeError, IndexError, AttributeError):
        return None


def _exifread_dms_to_deg(tag, ref_tag) -> float | None:
    if tag is None:
        return None
    try:
        vals = tag.values
        d = _exifread_ratio(vals[0]) or 0
        m = _exifread_ratio(vals[1]) or 0
        s = _exifread_ratio(vals[2]) or 0
        deg = d + m / 60 + s / 3600
        if ref_tag is not None and str(ref_tag) in ("S", "W"):
            deg = -deg
        return round(deg, 7)
    except (TypeError, IndexError, AttributeError):
        return None


def _extract_exif_rawfile(path: Path) -> dict:
    """RAWファイル向けexifreadフォールバック。出力キーはpiexif版と完全に
    揃える (呼び出し側のextract_one/exif保存ロジックは無変更でよい)。"""
    try:
        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False)
    except Exception:
        return {}
    if not tags:
        return {}

    out: dict = {}
    dt = tags.get("EXIF DateTimeOriginal") or tags.get("Image DateTime")
    if dt is not None:
        try:
            out["taken_at"] = datetime.strptime(
                str(dt), "%Y:%m:%d %H:%M:%S"
            ).isoformat()
        except (ValueError, TypeError):
            pass

    out["make"] = str(tags["Image Make"]) if "Image Make" in tags else None
    out["model"] = str(tags["Image Model"]) if "Image Model" in tags else None
    out["lens"] = str(tags["EXIF LensModel"]) if "EXIF LensModel" in tags else None
    out["focal"] = _exifread_scalar(tags.get("EXIF FocalLength"))
    out["f"] = _exifread_scalar(tags.get("EXIF FNumber"))
    out["ss"] = _format_shutter(_exifread_scalar(tags.get("EXIF ExposureTime")))
    iso_tag = tags.get("EXIF ISOSpeedRatings")
    if iso_tag is not None:
        try:
            out["iso"] = int(iso_tag.values[0])
        except (TypeError, IndexError, ValueError):
            pass

    if "GPS GPSLatitude" in tags:
        out["lat"] = _exifread_dms_to_deg(
            tags["GPS GPSLatitude"], tags.get("GPS GPSLatitudeRef"))
        out["lon"] = _exifread_dms_to_deg(
            tags["GPS GPSLongitude"], tags.get("GPS GPSLongitudeRef"))
        if "GPS GPSImgDirection" in tags:
            out["direction"] = _exifread_scalar(tags.get("GPS GPSImgDirection"))

    return {k: v for k, v in out.items() if v is not None}


def _extract_exif_piexif(path: Path) -> dict:
    try:
        ex = piexif.load(str(path))
    except Exception:
        return {}

    zeroth, exif_ifd, gps = ex.get("0th", {}), ex.get("Exif", {}), ex.get("GPS", {})
    out: dict = {}

    if dt := exif_ifd.get(piexif.ExifIFD.DateTimeOriginal) or zeroth.get(piexif.ImageIFD.DateTime):
        try:
            out["taken_at"] = datetime.strptime(
                _decode(dt), "%Y:%m:%d %H:%M:%S"
            ).isoformat()
        except (ValueError, TypeError):
            pass

    out["make"] = _decode(zeroth.get(piexif.ImageIFD.Make))
    out["model"] = _decode(zeroth.get(piexif.ImageIFD.Model))
    out["lens"] = _decode(exif_ifd.get(piexif.ExifIFD.LensModel))
    out["focal"] = _rational(exif_ifd.get(piexif.ExifIFD.FocalLength))
    out["f"] = _rational(exif_ifd.get(piexif.ExifIFD.FNumber))
    out["ss"] = _shutter(exif_ifd.get(piexif.ExifIFD.ExposureTime))
    iso = exif_ifd.get(piexif.ExifIFD.ISOSpeedRatings)
    out["iso"] = iso[0] if isinstance(iso, tuple) else iso

    if piexif.GPSIFD.GPSLatitude in gps:
        out["lat"] = _dms_to_deg(gps[piexif.GPSIFD.GPSLatitude],
                                 gps.get(piexif.GPSIFD.GPSLatitudeRef, b"N"))
        out["lon"] = _dms_to_deg(gps[piexif.GPSIFD.GPSLongitude],
                                 gps.get(piexif.GPSIFD.GPSLongitudeRef, b"E"))
        if piexif.GPSIFD.GPSImgDirection in gps:
            out["direction"] = _rational(gps[piexif.GPSIFD.GPSImgDirection])

    return {k: v for k, v in out.items() if v is not None}


def extract_exif(path: Path) -> dict:
    """EXIF を正規化 dict で返す。EXIF が無い/壊れている場合は空 dict。

    piexifを先に試し (一部のTIFFベースRAWはこれだけで取れる)、空だった場合
    かつRAWファイルのときのみexifreadにフォールバックする。
    """
    out = _extract_exif_piexif(path)
    if not out and is_raw(path.suffix):
        out = _extract_exif_rawfile(path)
    return out


def build_gps_exif(lat: float, lon: float) -> dict:
    """テスト画像生成用: 10進緯度経度 → piexif GPS IFD。"""
    def to_dms(deg: float):
        deg = abs(deg)
        d = int(deg)
        m = int((deg - d) * 60)
        s = Fraction((deg - d - m / 60) * 3600).limit_denominator(10000)
        return ((d, 1), (m, 1), (s.numerator, s.denominator))

    return {
        piexif.GPSIFD.GPSLatitudeRef: b"N" if lat >= 0 else b"S",
        piexif.GPSIFD.GPSLatitude: to_dms(lat),
        piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
        piexif.GPSIFD.GPSLongitude: to_dms(lon),
    }
