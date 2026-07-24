"""逆ジオコーディング (M4/M5): 完全オフライン。

M4: reverse_geocoder (GeoNames cities1000) で最寄り都市 → 都道府県は日本語化。
M5: OSM POI ローカル DB (data/poi.db, tools/build_poi_db.py で構築/更新) の
    近傍検索で建物・スポット名を推定する。
    - 半径 POI_RADIUS_M 以内で最も近い名前付き POI を採用
    - EXIF に撮影方位 (GPSImgDirection) があれば、カメラの向き ±POI_FOV_DEG に
      入らない遠方 POI を除外（背中側の施設名が付く誤りを減らす）
"""
from __future__ import annotations

import logging
import math
import sqlite3
import threading

from .db import LockedConnection
from .poi_fetch import POI_DB  # 定義元は poi_fetch.py (単一の定義元)

log = logging.getLogger("photofinder.geo")
POI_RADIUS_M = 150.0    # 主 POI: この距離以内のみ採用
POI_ALT_RADIUS_M = 250.0  # 周辺 POI (検索用の別名): 少し広めに拾う
POI_FOV_DEG = 60.0      # 撮影方位がある場合の視野 (±60°)。近距離 (<30m) では適用しない
_M_PER_DEG_LAT = 111_320.0

PREF_JA = {
    "Hokkaido": "北海道", "Aomori": "青森県", "Iwate": "岩手県", "Miyagi": "宮城県",
    "Akita": "秋田県", "Yamagata": "山形県", "Fukushima": "福島県",
    "Ibaraki": "茨城県", "Tochigi": "栃木県", "Gunma": "群馬県", "Saitama": "埼玉県",
    "Chiba": "千葉県", "Tokyo": "東京都", "Kanagawa": "神奈川県",
    "Niigata": "新潟県", "Toyama": "富山県", "Ishikawa": "石川県", "Fukui": "福井県",
    "Yamanashi": "山梨県", "Nagano": "長野県", "Gifu": "岐阜県",
    "Shizuoka": "静岡県", "Aichi": "愛知県", "Mie": "三重県", "Shiga": "滋賀県",
    "Kyoto": "京都府", "Osaka": "大阪府", "Hyogo": "兵庫県", "Nara": "奈良県",
    "Wakayama": "和歌山県", "Tottori": "鳥取県", "Shimane": "島根県",
    "Okayama": "岡山県", "Hiroshima": "広島県", "Yamaguchi": "山口県",
    "Tokushima": "徳島県", "Kagawa": "香川県", "Ehime": "愛媛県", "Kochi": "高知県",
    "Fukuoka": "福岡県", "Saga": "佐賀県", "Nagasaki": "長崎県",
    "Kumamoto": "熊本県", "Oita": "大分県", "Miyazaki": "宮崎県",
    "Kagoshima": "鹿児島県", "Okinawa": "沖縄県",
}

_lock = threading.Lock()
_loaded = False
_failed = False


def _ensure_loaded() -> bool:
    """初回に k-d tree を構築 (数秒)。失敗したら以後スキップ。"""
    global _loaded, _failed
    if _failed:
        return False
    with _lock:
        if not _loaded:
            try:
                import reverse_geocoder as rg
                rg.search([(35.0, 135.0)], mode=1)  # ウォームアップ兼動作確認
                _loaded = True
            except Exception:
                log.exception("reverse_geocoder init failed")
                _failed = True
    return _loaded


def reverse_geocode(lat: float, lon: float,
                    direction: float | None = None) -> dict | None:
    """{country, prefecture, city, poi_name, poi_conf, poi_alt} を返す。

    都市名 (reverse_geocoder) と POI (ローカル poi.db) は独立に引く。
    どちらか一方が使えない環境でも、取れた方だけの部分結果を返す。
    """
    country = prefecture = city = ""
    if _ensure_loaded():
        import reverse_geocoder as rg
        try:
            # mode=1: シングルプロセス (Windows のスレッド内から安全に呼ぶため)
            hit = rg.search([(lat, lon)], mode=1)[0]
            country = hit.get("cc", "")
            admin1 = hit.get("admin1", "")
            prefecture = PREF_JA.get(admin1, admin1) if country == "JP" else admin1
            city = hit.get("name", "")
        except Exception:
            log.exception("reverse_geocode failed (%s, %s)", lat, lon)

    poi = poi_lookup(lat, lon, direction)
    if not country and not poi:
        return None
    return {
        "country": country,
        "prefecture": prefecture,
        "city": city,
        "poi_name": poi["name"] if poi else None,
        "poi_conf": poi["conf"] if poi else None,
        "poi_alt": " ".join(poi["alts"]) if poi and poi["alts"] else None,
    }


# ------------------------------------------------------------ OSM POI ------

_poi_lock = threading.Lock()  # _poi_db の遅延オープン/差し替え自体を保護 (接続内部の直列化は LockedConnection が担当)
_poi_db: LockedConnection | None = None
_poi_missing = False


def _open_poi_db() -> LockedConnection | None:
    """data/poi.db を遅延オープン。ツールでの更新後は invalidate_poi_db() で再読込。

    scan スレッドと複数の API リクエストスレッドから同じ接続へ同時に
    execute()/fetchall() が飛んでくるため、db.py の main DB と同じ理由
    (2026-07-12 実機で "bad parameter or other API misuse" が発生) で
    LockedConnection で直列化する。素の sqlite3.Connection をそのまま
    共有してはいけない。
    """
    global _poi_db, _poi_missing
    with _poi_lock:
        if _poi_db is not None:
            return _poi_db
        if _poi_missing:
            return None
        if not POI_DB.exists():
            _poi_missing = True
            return None
        conn = sqlite3.connect(POI_DB, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _poi_db = LockedConnection(conn)
        return _poi_db


def invalidate_poi_db() -> None:
    """POI データ更新後に呼ぶと次回アクセス時に開き直す。"""
    global _poi_db, _poi_missing
    with _poi_lock:
        if _poi_db is not None:
            _poi_db.close()
        _poi_db = None
        _poi_missing = False


def poi_status() -> dict:
    db = _open_poi_db()
    if db is None:
        return {"available": False, "prefs": [], "total": 0}
    prefs = [dict(r) for r in db.execute("SELECT * FROM poi_meta ORDER BY pref")]
    total = db.execute("SELECT count(*) FROM pois").fetchone()[0]
    return {"available": True, "prefs": prefs, "total": total}


# 撮影地名として不適な POI (宿泊施設・案内所) と、施設の一部を指す汎用名。
# 例: 清水寺で最寄りが「本堂」(historic=yes, 8m) になり検索価値が無い問題への対策
EXCLUDE_KINDS = {
    "tourism=hotel", "tourism=guest_house", "tourism=hostel", "tourism=motel",
    "tourism=apartment", "tourism=chalet", "tourism=camp_pitch",
    "tourism=information",
}
GENERIC_NAMES = {
    "本堂", "本殿", "拝殿", "本坊", "社務所", "宗務本院", "手水舎", "鐘楼",
    "庫裏", "納経所", "案内所", "参集殿", "授与所", "トイレ", "駐車場",
    # 方角相対名 (親施設名なしでは検索語にならない)
    "東庭", "西庭", "南庭", "北庭", "中庭", "前庭",
}
MAX_ALTS = 5
# 種別の優先度: 施設の「代表名」になる種別を距離より優先する
KIND_RANK = {
    "amenity=place_of_worship": 3, "tourism=attraction": 3,
    "leisure=park": 3, "leisure=garden": 3, "leisure=nature_reserve": 3,
    "boundary=national_park": 3, "railway=station": 3,
    "tourism=viewpoint": 3, "tourism=museum": 3, "tourism=zoo": 3,
    "tourism=aquarium": 3, "tourism=theme_park": 3,
}


def _poi_rank(kind: str) -> int:
    if kind in EXCLUDE_KINDS:
        return -1
    if kind in KIND_RANK:
        return KIND_RANK[kind]
    if kind.startswith(("historic=", "natural=", "man_made=", "waterway=")):
        return 2 if kind != "historic=yes" else 1
    return 1


def poi_lookup(lat: float, lon: float,
               direction: float | None = None) -> dict | None:
    """{name, kind, dist_m, conf, alts} / 見つからなければ None。

    主 POI: 半径 150m 内で 種別優先度 (寺社・公園・駅など > その他) → 距離 の順に選ぶ。
    alts:  半径 250m 内の他の主要 POI 名 (最大3件)。検索用 —
           例: 境内の「地主神社」が主 POI でも「清水寺」で検索できるようにする。
           tourism=information (案内板) は主 POI にはしないが、名前は敷地名そのもの
           であることが多いため alts には採用する。
    """
    db = _open_poi_db()
    if db is None:
        return None
    dlat = POI_ALT_RADIUS_M / _M_PER_DEG_LAT
    dlon = POI_ALT_RADIUS_M / (_M_PER_DEG_LAT * max(0.2, math.cos(math.radians(lat))))
    rows = db.execute(
        """SELECT p.name, p.kind, p.lat, p.lon FROM poi_rtree r
           JOIN pois p ON p.id = r.id
           WHERE r.min_lat >= ? AND r.max_lat <= ?
             AND r.min_lon >= ? AND r.max_lon <= ?""",
        (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
    ).fetchall()

    best = None                       # (rank, -dist) 最大を主 POI に
    alt_cands: list[tuple] = []       # (rank, -dist, name)
    for r in rows:
        if r["name"] in GENERIC_NAMES:
            continue
        rank = _poi_rank(r["kind"])
        dist = _dist_m(lat, lon, r["lat"], r["lon"])
        if dist > POI_ALT_RADIUS_M:
            continue
        # 撮影方位フィルタ: 遠い POI はカメラの向いている方向のみ採用
        if direction is not None and dist > 30:
            bearing = _bearing_deg(lat, lon, r["lat"], r["lon"])
            if _angle_diff(bearing, direction) > POI_FOV_DEG:
                continue
        if rank >= 2 or r["kind"] == "tourism=information":
            alt_cands.append((max(rank, 0), -dist, r["name"]))
        if rank < 0 or dist > POI_RADIUS_M:
            continue
        key = (rank, -dist)
        if best is None or key > best[0]:
            best = (key, dist, r)
    if best is None:
        return None
    _, dist, r = best

    alts: list[str] = []
    for _, _, name in sorted(alt_cands, reverse=True):
        if name != r["name"] and name not in alts:
            alts.append(name)
        if len(alts) >= MAX_ALTS:
            break
    return {
        "name": r["name"], "kind": r["kind"], "dist_m": round(dist, 1),
        "conf": round(1.0 - dist / POI_RADIUS_M, 3),  # 近いほど高い
        "alts": alts,
    }


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """近距離用の平面近似 (150m スケールでは十分)。"""
    dy = (lat2 - lat1) * _M_PER_DEG_LAT
    dx = (lon2 - lon1) * _M_PER_DEG_LAT * math.cos(math.radians(lat1))
    return math.hypot(dx, dy)


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dy = lat2 - lat1
    dx = (lon2 - lon1) * math.cos(math.radians(lat1))
    return (math.degrees(math.atan2(dx, dy)) + 360) % 360


def _angle_diff(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)
