"""OSM POI データベースの構築・更新ツール (CLI)。

実体は photofinder/poi_fetch.py に共通化（実行中サーバの設定画面からも同じ処理を呼べる）。
本 CLI はサーバを起動せずまとめて複数県を取得したい場合に使う。

使い方:
    # 取得/更新 (都道府県名で指定。再実行するとその県のデータを丸ごと入れ替え = 更新)
    .venv\\Scripts\\python.exe tools\\build_poi_db.py 京都府 大阪府

    # 状態確認
    .venv\\Scripts\\python.exe tools\\build_poi_db.py --status

対象タグ: 観光地・史跡・公園・寺社・展望塔・駅など「写真の撮影地になりうる地点」。
飲食店等の大量 POI は意図的に除外（撮影地タグとしてノイズになるため）。
データは ODbL ライセンス (© OpenStreetMap contributors)。
アプリの 設定 → 場所データ から都道府県の追加/削除、カスタム地点の追加/削除もできる。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from photofinder.poi_fetch import (  # noqa: E402
    PREF_ISO, POI_DB, fetch_pref, open_poi_db, store_pref,
)


def show_status(db) -> None:
    rows = db.execute("SELECT * FROM poi_meta ORDER BY pref").fetchall()
    if not rows:
        print("POI データ未取得。例: python tools\\build_poi_db.py 京都府")
        return
    total = db.execute("SELECT count(*) FROM pois").fetchone()[0]
    for r in rows:
        print(f"  {r['pref']}: {r['count']:,}件 (取得 {r['fetched_at'][:10]})")
    print(f"  合計 {total:,}件 / {POI_DB} ({POI_DB.stat().st_size // 1024:,} KB)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("prefs", nargs="*", help="都道府県名 (例: 京都府 大阪府)")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    db = open_poi_db()
    if args.status or not args.prefs:
        show_status(db)
        return
    for pref in args.prefs:
        if pref not in PREF_ISO:
            print(f"不明な都道府県名: {pref}", file=sys.stderr)
            continue
        print(f"{pref}: Overpass API から取得中… (数十秒〜数分)")
        pois = fetch_pref(pref)
        store_pref(db, pref, pois)
        print(f"  -> {len(pois):,}件 保存")
        time.sleep(5)  # Overpass への連続リクエストを控えめに
    print("完了。写真への反映: アプリの 設定 →「POIを写真に再適用」"
          "(または POST /api/geo/refresh-poi)")


if __name__ == "__main__":
    main()
