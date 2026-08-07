"""SQLite 接続とスキーマ初期化。"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class _FetchedRows:
    """execute() が返す、全行を読み切り済みのカーソル互換オブジェクト。

    fetchone/fetchall/イテレーションのみ提供する (このコードベースで
    実際に使われている範囲)。DML文 (INSERT/UPDATE/DELETE) は行を返さない
    ため rows=[] になるだけで、lastrowid/rowcount はそのまま素通しする。
    """

    def __init__(self, rows: list, lastrowid, rowcount: int):
        self._rows = rows
        self._pos = 0
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self) -> list:
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rows

    def __iter__(self):
        return iter(self._rows[self._pos:])


class LockedConnection:
    """sqlite3.Connection のラッパー。呼び出しを単一の RLock で直列化する。

    FastAPI の同期エンドポイントはスレッドプールで並列実行されるため、
    プロセス内で1つだけ持つ Connection (main.py の db、poi_fetch 以外) に
    複数スレッドから同時に execute() が飛んでくる。check_same_thread=False は
    「別スレッドから使ってよい」という許可であって「同時に使っても安全」という
    保証ではない。

    当初は execute() 呼び出し自体だけを RLock で囲っていたが、それでも
    実機で写真グリッドの一斉サムネイル読み込み時に "bad parameter or other
    API misuse" や結果行への None 混入が間欠的に発生した (2026-07-12)。
    原因は Python の sqlite3 モジュールが内部で持つ文キャッシュ (同一SQL文字列の
    プリペアドステートメント使い回し) にあり、execute() 自体は直列化できても、
    戻り値の Cursor に対する fetchone()/fetchall() をロックの外で呼ぶと、
    その間に別スレッドが同じキャッシュ済み文を再利用してしまいうる。
    そのため execute() は行を全て読み切ってから (INSERT/UPDATE/DELETE なら
    空リストのまま) ロックを解放するようにした。呼び出し側から見た
    fetchone/fetchall/イテレーションの互換性は _FetchedRows で保つ。

    RLock なので、呼び出し側が `with db.lock:` で複数の execute() を
    まとめて直列化しても (例: main.py の _db_write ブロック) 内側の
    execute() が同じスレッドから再入でき、デッドロックしない。
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self.lock = threading.RLock()

    def execute(self, *args, **kwargs) -> _FetchedRows:
        with self.lock:
            cur = self._conn.execute(*args, **kwargs)
            return _FetchedRows(cur.fetchall(), cur.lastrowid, cur.rowcount)

    def executemany(self, *args, **kwargs):
        with self.lock:
            return self._conn.executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        with self.lock:
            return self._conn.executescript(*args, **kwargs)

    def commit(self):
        with self.lock:
            return self._conn.commit()

    def rollback(self):
        with self.lock:
            return self._conn.rollback()

    def close(self):
        with self.lock:
            return self._conn.close()


def open_db(data_dir: Path) -> LockedConnection:
    data_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(data_dir / "photofinder.db", check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA foreign_keys=ON")
    # API とスキャンは別接続 (WAL でも書き込みは1本)。ロック競合時に
    # 即エラーにせず待つ (database is locked → 500 を防ぐ)
    db.execute("PRAGMA busy_timeout=15000")
    db.row_factory = sqlite3.Row
    _migrate(db)
    db.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    db.commit()
    return LockedConnection(db)


def _migrate(db: sqlite3.Connection) -> None:
    """既存 DB を現行スキーマへ引き上げる。新規 DB は schema.sql がそのまま作る。"""
    row = db.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone() if _has(db, "schema_meta") else None
    version = int(row["value"]) if row else 0
    if version and version < 2:
        # v1 → v2: ml_version 列 + photos_fts を contentless から通常テーブルへ
        cols = [r["name"] for r in db.execute("PRAGMA table_info(photos)")]
        if "ml_version" not in cols:
            db.execute("ALTER TABLE photos ADD COLUMN ml_version INTEGER NOT NULL DEFAULT 0")
        db.execute("DROP TABLE IF EXISTS photos_fts")  # 未使用だったため作り直しで良い
        db.execute("UPDATE schema_meta SET value='2' WHERE key='schema_version'")
        db.commit()
        version = 2
    if version and version < 3:
        # v2 → v3: geo.poi_alt (周辺主要POI名、検索用)
        cols = [r["name"] for r in db.execute("PRAGMA table_info(geo)")]
        if "poi_alt" not in cols:
            db.execute("ALTER TABLE geo ADD COLUMN poi_alt TEXT")
        db.execute("UPDATE schema_meta SET value='3' WHERE key='schema_version'")
        db.commit()
        version = 3
    if version and version < 4:
        # v3 → v4: roots.recursive (サブフォルダを走査するか)
        cols = [r["name"] for r in db.execute("PRAGMA table_info(roots)")]
        if "recursive" not in cols:
            db.execute(
                "ALTER TABLE roots ADD COLUMN recursive INTEGER NOT NULL DEFAULT 1")
        db.execute("UPDATE schema_meta SET value='4' WHERE key='schema_version'")
        db.commit()
        version = 4
    if version and version < 5:
        # v4 → v5: photo_posts (X投稿リンク)。CREATE TABLE IF NOT EXISTS が
        # schema.sql 側で常に実行されるため ALTER 不要。バージョン番号だけ揃える
        db.execute("UPDATE schema_meta SET value='5' WHERE key='schema_version'")
        db.commit()
        version = 5
    if version and version < 6:
        # v5 → v6: photo_posts.platform/platform_label (投稿先SNS種別)、
        # photos.exported_at (書き出し済みマーク用)
        cols = [r["name"] for r in db.execute("PRAGMA table_info(photo_posts)")]
        if "platform" not in cols:
            db.execute(
                "ALTER TABLE photo_posts ADD COLUMN platform TEXT NOT NULL DEFAULT 'x' "
                "CHECK (platform IN ('x','instagram','other'))")
        if "platform_label" not in cols:
            db.execute("ALTER TABLE photo_posts ADD COLUMN platform_label TEXT")
        pcols = [r["name"] for r in db.execute("PRAGMA table_info(photos)")]
        if "exported_at" not in pcols:
            db.execute("ALTER TABLE photos ADD COLUMN exported_at TEXT")
        db.execute("UPDATE schema_meta SET value='6' WHERE key='schema_version'")
        db.commit()
        version = 6
    if version and version < 7:
        # v6 → v7: backup_auto (週次自動スナップショット設定) を削除。フル
        # バックアップ/復元への置き換えで参照されなくなったため後始末する
        db.execute("DELETE FROM app_settings WHERE key='backup_auto'")
        db.execute("UPDATE schema_meta SET value='7' WHERE key='schema_version'")
        db.commit()


def _has(db: sqlite3.Connection, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None
