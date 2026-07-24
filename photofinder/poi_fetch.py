"""OSM POI データの取得・管理 (tools/build_poi_db.py と共有)。

- 都道府県単位で Overpass API から取得（CLI からも実行中サーバからも呼べる）
- カスタム地点の手動追加/削除
- 名前検索（オートコンプリート・管理画面用）

data/poi.db は完全にローカル。取得後はオフラインで poi_lookup (geo.py) が使う。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .paths import data_dir

log = logging.getLogger("photofinder.poi_fetch")

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
UA = {"User-Agent": "PhotoFinder-personal/0.1 (local photo indexer)"}

PREF_ISO = {
    "北海道": "JP-01", "青森県": "JP-02", "岩手県": "JP-03", "宮城県": "JP-04",
    "秋田県": "JP-05", "山形県": "JP-06", "福島県": "JP-07", "茨城県": "JP-08",
    "栃木県": "JP-09", "群馬県": "JP-10", "埼玉県": "JP-11", "千葉県": "JP-12",
    "東京都": "JP-13", "神奈川県": "JP-14", "新潟県": "JP-15", "富山県": "JP-16",
    "石川県": "JP-17", "福井県": "JP-18", "山梨県": "JP-19", "長野県": "JP-20",
    "岐阜県": "JP-21", "静岡県": "JP-22", "愛知県": "JP-23", "三重県": "JP-24",
    "滋賀県": "JP-25", "京都府": "JP-26", "大阪府": "JP-27", "兵庫県": "JP-28",
    "奈良県": "JP-29", "和歌山県": "JP-30", "鳥取県": "JP-31", "島根県": "JP-32",
    "岡山県": "JP-33", "広島県": "JP-34", "山口県": "JP-35", "徳島県": "JP-36",
    "香川県": "JP-37", "愛媛県": "JP-38", "高知県": "JP-39", "福岡県": "JP-40",
    "佐賀県": "JP-41", "長崎県": "JP-42", "熊本県": "JP-43", "大分県": "JP-44",
    "宮崎県": "JP-45", "鹿児島県": "JP-46", "沖縄県": "JP-47",
}

# Overpass の抽出条件。「撮影地になりうる名前付き地点」に絞る
POI_SELECTORS = [
    '["tourism"]',                        # 観光地・展望台・美術館など
    '["historic"]',                       # 史跡・城・記念碑
    '["leisure"~"^(park|garden|nature_reserve|beach_resort|bird_hide)$"]',
    '["amenity"~"^(place_of_worship|aquarium|zoo|theatre|arts_centre|marketplace|university)$"]',
    '["natural"~"^(peak|beach|cape|spring|wetland|wood)$"]',
    '["man_made"~"^(tower|lighthouse|bridge|pier)$"]',
    '["railway"="station"]',
    '["waterway"~"^(waterfall|dam)$"]',
    '["boundary"="national_park"]',
]

POI_DB = data_dir() / "poi.db"  # geo.py はここから import する (単一の定義元)


def open_poi_db(path: Path = POI_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA busy_timeout=15000")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS pois (
            id       INTEGER PRIMARY KEY,
            osm_id   TEXT NOT NULL,          -- 'node/123' 'way/456' / 手動は 'manual/<uuid>'
            name     TEXT NOT NULL,
            kind     TEXT NOT NULL,          -- tourism=viewpoint 等の代表タグ / 手動は 'manual'
            pref     TEXT NOT NULL,          -- 取得単位 (入れ替え更新のキー) / 手動は 'manual'
            lat REAL NOT NULL, lon REAL NOT NULL,
            UNIQUE (pref, osm_id)
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS poi_rtree USING rtree(
            id, min_lat, max_lat, min_lon, max_lon
        );
        CREATE TABLE IF NOT EXISTS poi_meta (
            pref TEXT PRIMARY KEY,
            fetched_at TEXT NOT NULL,
            count INTEGER NOT NULL
        );
    """)
    return db


def fetch_pref(pref: str) -> list[dict]:
    """Overpass API から1都道府県分の名前付きPOIを取得する（ネットワークI/O、数十秒〜数分）。"""
    iso = PREF_ISO[pref]
    selectors = "\n".join(
        f'  nwr(area.a)["name"]{sel};' for sel in POI_SELECTORS)
    query = f"""[out:json][timeout:300];
area["ISO3166-2"="{iso}"][admin_level=4]->.a;
(
{selectors}
);
out center tags;"""
    body = urllib.parse.urlencode({"data": query}).encode()
    data = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(OVERPASS_URL, data=body, headers=UA)
            with urllib.request.urlopen(req, timeout=360) as r:
                data = json.load(r)
            break
        except Exception as e:
            if attempt == 2:
                raise
            wait = 30 * (attempt + 1)
            log.warning("overpass fetch retry in %ds (%s)", wait, e)
            time.sleep(wait)

    out = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name", "").strip()
        if not name or len(name) > 60:
            continue
        if el["type"] == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:
            c = el.get("center") or {}
            lat, lon = c.get("lat"), c.get("lon")
        if lat is None:
            continue
        kind = next(
            (f"{k}={tags[k]}" for k in
             ("tourism", "historic", "leisure", "amenity", "natural",
              "man_made", "railway", "waterway", "boundary") if k in tags),
            "unknown")
        out.append({"osm_id": f"{el['type']}/{el['id']}", "name": name,
                    "kind": kind, "lat": lat, "lon": lon})
    return out


def store_pref(db: sqlite3.Connection, pref: str, pois: list[dict]) -> None:
    """県単位で丸ごと入れ替え = 再実行がそのまま更新になる。"""
    old = [r["id"] for r in db.execute("SELECT id FROM pois WHERE pref=?", (pref,))]
    if old:
        db.executemany("DELETE FROM poi_rtree WHERE id=?", [(i,) for i in old])
        db.execute("DELETE FROM pois WHERE pref=?", (pref,))
    for p in pois:
        cur = db.execute(
            "INSERT OR IGNORE INTO pois (osm_id, name, kind, pref, lat, lon) "
            "VALUES (?,?,?,?,?,?)",
            (p["osm_id"], p["name"], p["kind"], pref, p["lat"], p["lon"]))
        if cur.lastrowid:
            db.execute("INSERT INTO poi_rtree VALUES (?,?,?,?,?)",
                       (cur.lastrowid, p["lat"], p["lat"], p["lon"], p["lon"]))
    db.execute("INSERT OR REPLACE INTO poi_meta VALUES (?,?,?)",
               (pref, datetime.now(timezone.utc).isoformat(), len(pois)))
    db.commit()


def delete_pref(db: sqlite3.Connection, pref: str) -> int:
    """指定都道府県のPOIデータを削除する。削除件数を返す。"""
    ids = [r["id"] for r in db.execute("SELECT id FROM pois WHERE pref=?", (pref,))]
    if ids:
        db.executemany("DELETE FROM poi_rtree WHERE id=?", [(i,) for i in ids])
        db.execute("DELETE FROM pois WHERE pref=?", (pref,))
    db.execute("DELETE FROM poi_meta WHERE pref=?", (pref,))
    db.commit()
    return len(ids)


def add_manual_poi(db: sqlite3.Connection, name: str, lat: float, lon: float) -> int:
    """ユーザ手動追加の地点。pref='manual' として保存し、既存の近傍検索に自動的に乗る。"""
    osm_id = f"manual/{uuid.uuid4().hex[:12]}"
    cur = db.execute(
        "INSERT INTO pois (osm_id, name, kind, pref, lat, lon) VALUES (?,?,?,?,?,?)",
        (osm_id, name.strip(), "manual", "manual", lat, lon))
    db.execute("INSERT INTO poi_rtree VALUES (?,?,?,?,?)",
              (cur.lastrowid, lat, lat, lon, lon))
    db.execute(
        "INSERT INTO poi_meta (pref, fetched_at, count) VALUES ('manual', ?, "
        "  (SELECT count(*) FROM pois WHERE pref='manual')) "
        "ON CONFLICT(pref) DO UPDATE SET fetched_at=excluded.fetched_at, "
        "  count=(SELECT count(*) FROM pois WHERE pref='manual')",
        (datetime.now(timezone.utc).isoformat(),))
    db.commit()
    return cur.lastrowid


def delete_poi(db: sqlite3.Connection, poi_id: int) -> bool:
    row = db.execute("SELECT pref FROM pois WHERE id=?", (poi_id,)).fetchone()
    if not row:
        return False
    db.execute("DELETE FROM poi_rtree WHERE id=?", (poi_id,))
    db.execute("DELETE FROM pois WHERE id=?", (poi_id,))
    if row["pref"] == "manual":
        db.execute(
            "UPDATE poi_meta SET count=(SELECT count(*) FROM pois WHERE pref='manual') "
            "WHERE pref='manual'")
    db.commit()
    return True


def search_pois(db: sqlite3.Connection, q: str, limit: int = 20) -> list[dict]:
    rows = db.execute(
        "SELECT id, name, kind, pref, lat, lon FROM pois WHERE name LIKE ? "
        "ORDER BY name LIMIT ?", (f"%{q}%", limit)).fetchall()
    return [dict(r) for r in rows]


def poi_status(db: sqlite3.Connection) -> dict:
    rows = db.execute("SELECT * FROM poi_meta ORDER BY pref").fetchall()
    total = db.execute("SELECT count(*) FROM pois").fetchone()[0]
    return {"available": bool(rows), "prefs": [dict(r) for r in rows], "total": total}


# ------------------------------------------------------- バックグラウンド取得 --

@dataclass
class FetchStatus:
    running: bool = False
    pref: str = ""
    error: str | None = None
    done_prefs: list[str] = field(default_factory=list)

    def snapshot(self) -> dict:
        return {"running": self.running, "pref": self.pref, "error": self.error}


STATUS = FetchStatus()
_fetch_lock = threading.Lock()


def fetch_and_store(pref: str, poi_db_path: Path = POI_DB) -> dict:
    """都道府県1件を取得してDBへ格納。バックグラウンドスレッドから呼ぶ想定。"""
    if not _fetch_lock.acquire(blocking=False):
        return {"started": False, "reason": "fetch already running"}
    STATUS.running, STATUS.pref, STATUS.error = True, pref, None
    try:
        pois = fetch_pref(pref)
        db = open_poi_db(poi_db_path)
        try:
            store_pref(db, pref, pois)
        finally:
            db.close()
        STATUS.done_prefs.append(pref)
        return {"started": True, "pref": pref, "count": len(pois)}
    except Exception as e:
        log.exception("poi fetch failed: %s", pref)
        STATUS.error = str(e)
        return {"started": True, "pref": pref, "error": str(e)}
    finally:
        STATUS.running = False
        _fetch_lock.release()
