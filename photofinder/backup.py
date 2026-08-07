"""フルバックアップ: データディレクトリ全体のzip書き出しと復元。

方針:
  - DB (photofinder.db) は VACUUM main INTO で圧縮しながら書き出す
    (旧・週次スナップショット機能と同じ手法を流用 — 専用接続でスキャン
    スレッドのトランザクションと衝突せず、WAL下でも一貫したスナップショットが
    取れる)
  - vectors.faiss・poi.db(+sidecar)・species_bank.npz・color_bank.npz・
    thumbs/・previews/ はそのままファイルコピー。poi.db はWALではなく既定の
    rollback-journalモード (poi_fetch.open_poi_db が journal_mode を変更
    していないため) で、コミット後は -journal サイドカーが残らないので
    チェックポイント不要
  - tmp/ (Xアーカイブ取り込みの一時領域) と backup/ (このモジュール自身の
    退避先) は対象外

復元 (extract_full_backup) は vectors.faiss がプロセス起動時に一度だけ
メモリへ読み込まれ、以後ディスクから再読込されない (vectors.py 参照) ため、
生きたプロセスの下でファイルだけ差し替えても検索結果には反映されない。
そのため DB 単体だった旧 restore() と同じ「ファイル差し替え→プロセス終了→
Docker の restart policy で再起動」パターンを、データディレクトリ全体に
拡張して踏襲する (実際の os._exit は main.py 側の責務、このモジュールは
ファイル操作のみ担当)。

復元前には現在のライブファイルを `backup/before_restore_<timestamp>/` へ
コピーではなく rename (同一ファイルシステム上で瞬時・追加ディスク不要) で
退避する。直前の1世代のみ保持 (稀な手動操作のため世代管理は不要と判断)。
アップロードされたzipの検証は退避より前に完了させ、不正なアップロードが
ライブデータに触れないようにする。展開中に失敗したら退避したファイルを
書き戻し (ベストエフォート)、プロセスは再起動せずそのまま動作を続ける。
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import stat
import threading
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger("photofinder.backup")

MANIFEST_NAME = "backup_manifest.json"
DB_ARCNAME = "photofinder.db"
BACKUP_FORMAT_VERSION = 1

# DB以外でデータディレクトリ直下に置かれうるファイル/ディレクトリ。
# build_full_backup() (zipへ入れる) と stage_aside_current_data() (退避する)
# の両方がここを単一の定義元として使う — 2つの処理が食い違うと、退避だけ
# されてバックアップに含まれない/その逆、という事故になるため
_OPTIONAL_FILES = [
    "vectors.faiss",
    "poi.db", "poi.db-wal", "poi.db-shm", "poi.db-journal",
    "species_bank.npz", "color_bank.npz",
]
_DIRS = ["thumbs", "previews"]

_lock = threading.Lock()


def backup_dir(data_dir: Path) -> Path:
    return data_dir / "backup"


def try_acquire() -> bool:
    """アップロード受付時に main.py が呼ぶ。取得できたら run_full_restore() の
    finally で解放されるまで保持される (archive_import.py と同じ流儀)。"""
    return _lock.acquire(blocking=False)


def release() -> None:
    """try_acquire() 成功後、何らかの理由で run_full_restore() を呼べなかった
    場合に呼び出し側が使う解放関数。通常は run_full_restore() 内の finally が担当する。"""
    _lock.release()


@dataclass
class RestoreStatus:
    running: bool = False
    phase: str = "idle"  # idle | validating | staging | extracting | done | error
    error: str | None = None

    def snapshot(self) -> dict:
        return {"running": self.running, "phase": self.phase, "error": self.error}


STATUS = RestoreStatus()


def build_full_backup(data_dir: Path, out_path: Path) -> dict:
    """データディレクトリ全体を out_path (zip) へ書き出す。

    呼び出し側の責務: vstore.save() を先に呼んでおく (メモリ上の最新FAISS
    索引をディスクへ反映してからzipに含めるため)、scanner.STATUS.running を
    チェックする、out_path は DATA_DIR の外 (tempfile) に置く。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_files = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        db_tmp = out_path.with_suffix(".db.tmp")
        conn = sqlite3.connect(data_dir / "photofinder.db")
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("VACUUM main INTO ?", (str(db_tmp),))
        finally:
            conn.close()
        try:
            zf.write(db_tmp, DB_ARCNAME)
            n_files += 1
        finally:
            db_tmp.unlink(missing_ok=True)

        for name in _OPTIONAL_FILES:
            p = data_dir / name
            if p.is_file():
                zf.write(p, name)
                n_files += 1

        for dname in _DIRS:
            d = data_dir / dname
            if not d.is_dir():
                continue
            for f in d.rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(data_dir).as_posix())
                    n_files += 1

        manifest = {
            "format_version": BACKUP_FORMAT_VERSION,
            "created_at": datetime.now().isoformat(),
        }
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False))

    return {"files": n_files}


def validate_backup_zip(zf: zipfile.ZipFile, data_dir: Path) -> None:
    """アップロードされたバックアップzipを検証する。問題があれば ValueError。

    - backup_manifest.json / photofinder.db が存在すること
    - 各メンバーの展開先パスが data_dir の外を指さないこと (zip-slip対策)
    - 絶対パス・シンボリックリンクのメンバーを含まないこと
    """
    names = set(zf.namelist())
    if MANIFEST_NAME not in names:
        raise ValueError(f"{MANIFEST_NAME} not found - not a PhotoFinder backup")
    if DB_ARCNAME not in names:
        raise ValueError(f"{DB_ARCNAME} not found in backup")

    data_root = data_dir.resolve()
    for info in zf.infolist():
        if info.is_dir():
            continue
        if Path(info.filename).is_absolute():
            raise ValueError(f"absolute path member rejected: {info.filename}")
        target = (data_dir / info.filename).resolve()
        try:
            target.relative_to(data_root)
        except ValueError:
            raise ValueError(f"member escapes data directory: {info.filename}")
        is_symlink = stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)
        if is_symlink:
            raise ValueError(f"symlink member rejected: {info.filename}")


def stage_aside_current_data(data_dir: Path) -> Path:
    """現在のライブデータを backup_dir(data_dir)/before_restore_<timestamp>/ へ退避する。

    コピーではなく rename (同一ファイルシステム上なら瞬時・追加ディスク不要)。
    存在しない項目はスキップする。直前の退避世代は上書き前に削除する
    (稀な手動操作のため世代管理は不要 — 1世代のみで十分)。
    """
    bdir = backup_dir(data_dir)
    bdir.mkdir(parents=True, exist_ok=True)
    for old in bdir.glob("before_restore_*"):
        shutil.rmtree(old, ignore_errors=True)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    staged = bdir / f"before_restore_{ts}"
    staged.mkdir(parents=True)

    items = [DB_ARCNAME, "photofinder.db-wal", "photofinder.db-shm"] + _OPTIONAL_FILES + _DIRS
    for name in items:
        src = data_dir / name
        if src.exists():
            src.rename(staged / name)
    return staged


def rollback_staged(data_dir: Path, staged_dir: Path) -> None:
    """stage_aside_current_data() の退避を書き戻す (ベストエフォート、例外を投げない)。"""
    try:
        for src in staged_dir.iterdir():
            dest = data_dir / src.name
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest, ignore_errors=True)
                else:
                    dest.unlink(missing_ok=True)
            src.rename(dest)
        shutil.rmtree(staged_dir, ignore_errors=True)
    except Exception:
        log.exception("rollback_staged failed - manual recovery from %s may be needed", staged_dir)


def extract_full_backup(data_dir: Path, zip_path: Path) -> None:
    """検証済みのバックアップzipを data_dir へ展開する (呼び出し前に stage_aside 済みであること)。"""
    with zipfile.ZipFile(zip_path) as zf:
        validate_backup_zip(zf, data_dir)
        for info in zf.infolist():
            if info.is_dir() or info.filename == MANIFEST_NAME:
                continue
            zf.extract(info, data_dir)


def run_full_restore(data_dir: Path, zip_path: Path, db) -> None:
    """バックグラウンドスレッドから呼ぶ想定。結果は STATUS に反映する。

    db は .execute() を持つ接続 (main.py の LockedConnection)。生きたWALの
    内容を確定させてから退避するため (空でないWALを残したままphotofinder.db
    本体だけ退避すると、退避先を後で使う際に古い内容が混ざりうる)。

    呼び出し前に try_acquire() でロックを取得済みであること。os._exit は
    呼ばない (プロセス管理は main.py 側の責務 — STATUS.phase=="done" を見て
    再起動をスケジュールする)。
    """
    STATUS.__init__()  # 前回分をリセット (scanner.STATUS/archive_import.STATUS と同じ流儀)
    STATUS.running, STATUS.phase = True, "validating"
    try:
        with zipfile.ZipFile(zip_path) as zf:
            validate_backup_zip(zf, data_dir)
        STATUS.phase = "staging"
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        staged = stage_aside_current_data(data_dir)
        STATUS.phase = "extracting"
        try:
            extract_full_backup(data_dir, zip_path)
        except Exception:
            rollback_staged(data_dir, staged)
            raise
        STATUS.phase = "done"
    except Exception as e:
        log.exception("full restore failed")
        STATUS.error = str(e)
        STATUS.phase = "error"
    finally:
        STATUS.running = False
        release()
