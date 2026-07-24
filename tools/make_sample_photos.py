"""動作検証用サンプル写真の生成。

EXIF（日時・カメラ・レンズ・露出・GPS）付きの JPEG を sample-photos/ に作る。
実写ではなくグラデーション画像だが、インデックス/検索/詳細表示の検証には十分。
"""
from __future__ import annotations

import sys
from pathlib import Path

import piexif
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent.parent))
from photofinder.exif_utils import build_gps_exif  # noqa: E402

OUT = Path(__file__).parent.parent / "sample-photos"

SAMPLES = [
    # (filename, size, colors, dt, camera, lens, focal, f, exposure, iso, gps)
    ("2025-11-03_kamogawa_kingfisher.jpg", (1600, 1067), ("#1d6d8c", "#d98e3f"),
     "2025:11:03 07:12:44", "ILCE-7M4", "FE 200-600mm F5.6-6.3 G OSS",
     600, 6.3, (1, 1600), 1600, (35.0301, 135.7735)),
    ("2025-11-03_kamogawa_heron.jpg", (1600, 1067), ("#8fb5b8", "#3c5a63"),
     "2025:11:03 07:45:02", "ILCE-7M4", "FE 200-600mm F5.6-6.3 G OSS",
     400, 6.3, (1, 800), 800, (35.0303, 135.7738)),
    ("2025-11-04_tofukuji_autumn.jpg", (1600, 1067), ("#b8622e", "#7d3c21"),
     "2025:11:04 10:20:15", "ILCE-7M4", "FE 24-105mm F4 G OSS",
     35, 8.0, (1, 250), 200, (34.9763, 135.7740)),
    ("2025-11-04_kiyomizu.jpg", (1067, 1600), ("#9a8570", "#43382e"),
     "2025:11:04 14:05:33", "ILCE-7M4", "FE 24-105mm F4 G OSS",
     24, 5.6, (1, 500), 100, (34.9949, 135.7850)),
    ("2026-01-15_yodogawa_ducks.jpg", (1600, 1067), ("#57808c", "#2e373d"),
     "2026:01:15 09:30:00", "ILCE-7M4", "FE 200-600mm F5.6-6.3 G OSS",
     600, 7.1, (1, 2000), 2500, (34.7205, 135.4959)),
    ("2026-03-21_sakura_mejiro.jpg", (1600, 1067), ("#d9a0b0", "#7ba05b"),
     "2026:03:21 11:11:11", "ILCE-7M4", "FE 200-600mm F5.6-6.3 G OSS",
     560, 6.3, (1, 1250), 640, (34.6937, 135.5023)),
    ("2026-05-10_street_snap.jpg", (1600, 1067), ("#6c7a84", "#2e373d"),
     "2026:05:10 17:40:21", "RICOH GR IIIx", None,
     26, 2.8, (1, 320), 400, None),
    ("screenshot_no_exif.png", (1200, 800), ("#4a5568", "#1a202c"),
     None, None, None, None, None, None, None, None),
]


def make_image(size, colors, label):
    img = Image.new("RGB", size, colors[0])
    draw = ImageDraw.Draw(img)
    w, h = size
    c1 = tuple(int(colors[0][i:i+2], 16) for i in (1, 3, 5))
    c2 = tuple(int(colors[1][i:i+2], 16) for i in (1, 3, 5))
    for y in range(h):
        t = y / h
        draw.line([(0, y), (w, y)],
                  fill=tuple(int(a + (b - a) * t) for a, b in zip(c1, c2)))
    draw.ellipse([w*0.42, h*0.35, w*0.58, h*0.55], outline="white", width=4)
    draw.text((20, h - 44), label, fill="white")
    return img


def main():
    OUT.mkdir(exist_ok=True)
    for (name, size, colors, dt, model, lens, focal, f, exp, iso, gps) in SAMPLES:
        img = make_image(size, colors, name)
        path = OUT / name
        if name.endswith(".png"):
            img.save(path, "PNG")
            print("wrote", path.name, "(no exif)")
            continue

        zeroth = {piexif.ImageIFD.Make: b"SONY" if model and model.startswith("ILCE") else b"RICOH"}
        if model:
            zeroth[piexif.ImageIFD.Model] = model.encode()
        exif_ifd = {}
        if dt:
            exif_ifd[piexif.ExifIFD.DateTimeOriginal] = dt.encode()
        if lens:
            exif_ifd[piexif.ExifIFD.LensModel] = lens.encode()
        if focal:
            exif_ifd[piexif.ExifIFD.FocalLength] = (int(focal), 1)
        if f:
            exif_ifd[piexif.ExifIFD.FNumber] = (int(f * 10), 10)
        if exp:
            exif_ifd[piexif.ExifIFD.ExposureTime] = exp
        if iso:
            exif_ifd[piexif.ExifIFD.ISOSpeedRatings] = iso
        gps_ifd = build_gps_exif(*gps) if gps else {}

        exif_bytes = piexif.dump({"0th": zeroth, "Exif": exif_ifd, "GPS": gps_ifd})
        img.save(path, "JPEG", quality=90, exif=exif_bytes)
        print("wrote", path.name)


if __name__ == "__main__":
    main()
