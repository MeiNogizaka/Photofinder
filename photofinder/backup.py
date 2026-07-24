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
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("photofinder.backup")

KEEP_GENERATIONS = 3
INTERVAL_DAYS = 7
# ミリ秒部分は任意 (旧ファイル名との後方互換)。秒精度だけだと同じ秒内に
# snapshot() が2回呼ばれた場合にファイル名が衝突し、VACUUM INTO が
# "output file already exists" で失敗する (/api/backup/restore が復元の
# たびに安全スナップショットを自動作成するようになって以降、実機で発生を確認)
_NAME_RE = re.compile(r"^photofinder-(\d{8}-\d{6})(?:-(\d{3}))?\.db$")
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
        now = datetime.now()
        name = f"photofinder-{now.strftime('%Y%m%d-%H%M%S')}-{now.microsecond // 1000:03d}.db"
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


def validate_snapshot_path(data_dir: Path, snapshot_path: Path) -> Path:
    """snapshot_path が backup_dir 配下の実在ファイルであることを確認し、
    解決済みパスを返す。呼び出し側 (main.py) が安全スナップショットを作る
    "前に" 検証を済ませておくためのもの — restore() 内でも同じ検証を
    (安全のため) もう一度行うが、無効なリクエストのたびに無駄な安全
    スナップショットを作らずに済むよう先出しできるようにしてある。
    """
    snapshot_path = snapshot_path.resolve()
    try:
        snapshot_path.relative_to(backup_dir(data_dir).resolve())
    except ValueError:
        raise ValueError("snapshot_path must be inside the backup directory")
    if not snapshot_path.is_file():
        raise FileNotFoundError(str(snapshot_path))
    return snapshot_path


def restore(data_dir: Path, snapshot_path: Path, db) -> dict:
    """指定したスナップショットで photofinder.db を置き換える。

    呼び出し側 (main.py の /api/backup/restore) の責務: 復元前に safety
    スナップショットを別途 snapshot() で取ること、スキャン実行中は呼ばないこと、
    復元後にプロセスを終了しコンテナの restart policy で再起動させること
    (生きたWALモード接続を持ったままDBファイルを差し替えるのは危険なため、
    /api/shutdown と同じ「差し替え→即終了→再起動時に新しい状態で開き直す」
    パターンに乗る)。

    db は .execute() を持つ接続 (sqlite3.Connection でも db.LockedConnection
    でもよい、ダックタイピング) — 差し替え前に生きているWALの内容を確定させる
    のに使う。空でないWALを残したままphotofinder.db本体だけ差し替えると、
    次回起動時に無関係な (差し替え前のDBに対する) WAL内容を誤って適用されうる。
    """
    snapshot_path = validate_snapshot_path(data_dir, snapshot_path)

    with _lock:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target = data_dir / "photofinder.db"
        shutil.copyfile(snapshot_path, target)
        for suffix in ("-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
        log.info("restored from snapshot: %s", snapshot_path)
        return {"restored_from": str(snapshot_path)}


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
