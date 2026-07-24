"""バックアップ (M5): SQLite スナップショットの作成と世代管理。

方針 (docs/design.md §8):
  - SQLite の `VACUUM INTO` でオンラインスナップショット (稼働中でも安全・断片化も解消)
  - data/backup/photofinder-YYYYMMDD-HHMMSS.db、保持は KEEP_GENERATIONS 世代
  - FAISS・サムネは DB から再生成可能なため対象外。守るのは DB のみ
  - 自動実行は「起動時に前回から INTERVAL_DAYS 以上経過していたら」方式
    (常駐アプリではないため cron 的スケジューラは持たない)
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("photofinder.backup")

KEEP_GENERATIONS = 3
INTERVAL_DAYS = 7
_NAME_RE = re.compile(r"^photofinder-(\d{8}-\d{6})\.db$")
_lock = threading.Lock()


def backup_dir(data_dir: Path) -> Path:
    return data_dir / "backup"


def list_snapshots(data_dir: Path) -> list[dict]:
    """新しい順の [{path, taken_at, size}]"""
    d = backup_dir(data_dir)
    if not d.exists():
        return []
    out = []
    for f in d.iterdir():
        m = _NAME_RE.match(f.name)
        if not m:
            continue  # 想定外のファイルには触れない (prune でも削除しない)
        out.append({
            "path": str(f),
            "taken_at": datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").isoformat(),
            "size": f.stat().st_size,
        })
    return sorted(out, key=lambda x: x["taken_at"], reverse=True)


def snapshot(data_dir: Path) -> dict:
    """スナップショットを1つ作成し、古い世代を KEEP_GENERATIONS まで削減。

    アプリ本体とは別の専用接続で VACUUM INTO する。共有接続を使うと
    スキャンスレッドのトランザクションと衝突する (commit/VACUUM の競合) ため。
    WAL モードなので稼働中でも一貫したスナップショットが取れる。
    """
    with _lock:
        d = backup_dir(data_dir)
        d.mkdir(parents=True, exist_ok=True)
        name = f"photofinder-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
        out = d / name
        conn = sqlite3.connect(data_dir / "photofinder.db")
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("VACUUM main INTO ?", (str(out),))
        finally:
            conn.close()
        log.info("backup snapshot: %s (%d KB)", out, out.stat().st_size // 1024)

        for old in list_snapshots(data_dir)[KEEP_GENERATIONS:]:
            Path(old["path"]).unlink(missing_ok=True)
            log.info("backup pruned: %s", old["path"])
        return {"out_path": str(out), "size": out.stat().st_size}


def auto_backup_if_due(data_dir: Path) -> bool:
    """前回スナップショットから INTERVAL_DAYS 以上経過していれば作成。実行したら True。"""
    snaps = list_snapshots(data_dir)
    if snaps:
        last = datetime.fromisoformat(snaps[0]["taken_at"])
        if datetime.now() - last < timedelta(days=INTERVAL_DAYS):
            return False
    try:
        snapshot(data_dir)
        return True
    except Exception:
        log.exception("auto backup failed")
        return False
