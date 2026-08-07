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
コピーではなく rename (同一ファイルシステム上で瞬時) で退避する。rename
自体は追加ディスク不要だが、展開中は「退避済み旧ツリー + 新ツリー」が
同時に存在するため、ピーク空き容量はおおよそバックアップ展開後サイズ分
(非圧縮合計) が必要。直前の1世代のみ保持。アップロードされたzipの検証は
退避より前に完了させ、不正なアップロードがライブデータに触れないようにする。
展開中に失敗したら退避したファイルを書き戻し (ベストエフォート)、プロセスは
再起動せずそのまま動作を続ける。

途中クラッシュ対策: 退避開始前に `backup/RESTORE_IN_PROGRESS` マーカーを書き、
成功またはロールバック完了時に消す。起動時にマーカーが残っている、または
`photofinder.db` が無く `before_restore_*` だけがある場合は空DBを作らずに
最新の退避を書き戻す (recover_incomplete_restore)。

バックアップ作成と復元は同じ非ブロッキングロックで直列化し、スキャン等の
重い書き込みと同時に走らないよう main.py 側が is_busy() を参照する。
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
RESTORE_MARKER_NAME = "RESTORE_IN_PROGRESS"
# 展開時の余裕 (inode/ディレクトリ・一時ファイル用)。非圧縮合計に加算する
_FREE_SPACE_MARGIN = 64 * 1024 * 1024

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
# 退避時のみ対象 (バックアップzipには通常含めない WAL サイドカー)
_STAGE_EXTRA = ["photofinder.db-wal", "photofinder.db-shm"]

_lock = threading.Lock()
_current_op: str | None = None  # "backup" | "restore" | None


def backup_dir(data_dir: Path) -> Path:
    return data_dir / "backup"


def try_acquire(op: str = "restore") -> bool:
    """バックアップ作成または復元の排他ロックを取る。

    取得できたら対応する release() まで保持される (archive_import.py と同じ流儀)。
    op="restore" のときは STATUS を uploading にセットし、ポーリングが古い
    error を拾わないようにする (ロック取得直後 = リクエスト受付時)。
    """
    global _current_op
    if not _lock.acquire(blocking=False):
        return False
    _current_op = op
    if op == "restore":
        STATUS.running = True
        STATUS.phase = "uploading"
        STATUS.error = None
    return True


def release() -> None:
    """try_acquire() 成功後の解放。run_full_restore の finally、または
    スレッド起動前の失敗パスから呼ばれる。"""
    global _current_op
    if _current_op == "restore" and STATUS.phase == "uploading":
        # アップロード途中で失敗した場合のみ idle に戻す
        STATUS.running = False
        STATUS.phase = "idle"
    _current_op = None
    _lock.release()


def is_busy() -> bool:
    """フルバックアップ作成中または復元中なら True。スキャン等のゲートに使う。"""
    return _lock.locked()


def current_op() -> str | None:
    return _current_op


@dataclass
class RestoreStatus:
    running: bool = False
    phase: str = "idle"  # idle | uploading | validating | staging | extracting | done | error
    error: str | None = None

    def snapshot(self) -> dict:
        return {"running": self.running, "phase": self.phase, "error": self.error}


STATUS = RestoreStatus()


def _is_allowed_member(name: str) -> bool:
    """バックアップzipに含めてよい相対パスか。backup/ や tmp/ への書き込みを拒否。"""
    if name in (MANIFEST_NAME, DB_ARCNAME) or name in _OPTIONAL_FILES:
        return True
    # ディレクトリエントリ (末尾 /) は thumbs/ previews/ のみ許可
    stripped = name.rstrip("/")
    if stripped in _DIRS:
        return True
    parts = Path(name).parts
    if not parts:
        return False
    if parts[0] in _DIRS and ".." not in parts:
        return True
    return False


def build_full_backup(data_dir: Path, out_path: Path) -> dict:
    """データディレクトリ全体を out_path (zip) へ書き出す。

    呼び出し側の責務: vstore.save() を先に呼んでおく (メモリ上の最新FAISS
    索引をディスクへ反映してからzipに含めるため)、scanner / is_busy を
    チェックする、out_path は DATA_DIR の外 (tempfile) に置く、try_acquire
    ("backup") でロックを保持する。
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


def validate_backup_zip(zf: zipfile.ZipFile, data_dir: Path) -> dict:
    """アップロードされたバックアップzipを検証する。問題があれば ValueError。

    - backup_manifest.json / photofinder.db が存在すること
    - format_version が対応バージョンであること
    - メンバーが allowlist (DB / オプションファイル / thumbs|previews 配下) のみ
    - 各メンバーの展開先パスが data_dir の外を指さないこと (zip-slip対策)
    - 絶対パス・シンボリックリンクのメンバーを含まないこと

    戻り値: パース済み manifest dict。
    """
    names = set(zf.namelist())
    if MANIFEST_NAME not in names:
        raise ValueError(f"{MANIFEST_NAME} not found - not a PhotoFinder backup")
    if DB_ARCNAME not in names:
        raise ValueError(f"{DB_ARCNAME} not found in backup")

    try:
        manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as ex:
        raise ValueError(f"invalid {MANIFEST_NAME}: {ex}") from ex
    ver = manifest.get("format_version")
    if ver != BACKUP_FORMAT_VERSION:
        raise ValueError(
            f"unsupported backup format_version: {ver!r} "
            f"(this app supports {BACKUP_FORMAT_VERSION})"
        )

    data_root = data_dir.resolve()
    for info in zf.infolist():
        name = info.filename
        if info.is_dir():
            # 空ディレクトリエントリも allowlist 配下のみ
            if name.rstrip("/") and not _is_allowed_member(name):
                raise ValueError(f"unexpected directory member rejected: {name}")
            continue
        if Path(name).is_absolute() or name.startswith("/") or name.startswith("\\"):
            raise ValueError(f"absolute path member rejected: {name}")
        if not _is_allowed_member(name):
            raise ValueError(f"unexpected member rejected: {name}")
        # zip-slip: resolve 後も data_dir 内に収まること
        target = (data_dir / name).resolve()
        try:
            target.relative_to(data_root)
        except ValueError as ex:
            raise ValueError(f"member escapes data directory: {name}") from ex
        is_symlink = stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)
        if is_symlink:
            raise ValueError(f"symlink member rejected: {name}")

    return manifest


def _restore_marker_path(data_dir: Path) -> Path:
    return backup_dir(data_dir) / RESTORE_MARKER_NAME


def _write_restore_marker(data_dir: Path, staged_name: str) -> None:
    bdir = backup_dir(data_dir)
    bdir.mkdir(parents=True, exist_ok=True)
    _restore_marker_path(data_dir).write_text(
        json.dumps({"staged": staged_name, "at": datetime.now().isoformat()}),
        encoding="utf-8",
    )


def _clear_restore_marker(data_dir: Path) -> None:
    _restore_marker_path(data_dir).unlink(missing_ok=True)


def estimate_extract_bytes(zf: zipfile.ZipFile) -> int:
    """展開に要するおおよそのバイト数 (非圧縮合計 + マージン)。"""
    total = sum(info.file_size for info in zf.infolist() if not info.is_dir())
    return total + _FREE_SPACE_MARGIN


def check_free_space(data_dir: Path, needed: int) -> None:
    """data_dir があるファイルシステムに needed バイト以上の空きが無ければ ValueError。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(data_dir).free
    if free < needed:
        raise ValueError(
            f"insufficient free space for restore: need about {needed} bytes, "
            f"have {free} bytes free under {data_dir}"
        )


def stage_aside_current_data(data_dir: Path) -> Path:
    """現在のライブデータを backup_dir(data_dir)/before_restore_<timestamp>/ へ退避する。

    コピーではなく rename (同一ファイルシステム上なら瞬時)。存在しない項目は
    スキップ。途中で失敗したら、その時点までに動かした項目を書き戻してから
    例外を再送出する。直前の退避世代の削除は、新ステージが完全に成功してから
    行う (途中失敗で直前世代まで失わないため)。
    """
    bdir = backup_dir(data_dir)
    bdir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    staged = bdir / f"before_restore_{ts}"
    staged.mkdir(parents=True)

    items = [DB_ARCNAME] + _STAGE_EXTRA + _OPTIONAL_FILES + _DIRS
    moved: list[str] = []
    try:
        for name in items:
            src = data_dir / name
            if src.exists():
                src.rename(staged / name)
                moved.append(name)
    except Exception:
        for name in reversed(moved):
            src = staged / name
            dest = data_dir / name
            try:
                if dest.exists():
                    if dest.is_dir():
                        shutil.rmtree(dest, ignore_errors=True)
                    else:
                        dest.unlink(missing_ok=True)
                src.rename(dest)
            except Exception:
                log.exception("partial stage rollback failed for %s", name)
        shutil.rmtree(staged, ignore_errors=True)
        raise

    # 新ステージ完了後に古い世代だけ削除
    for old in bdir.glob("before_restore_*"):
        if old.resolve() != staged.resolve():
            shutil.rmtree(old, ignore_errors=True)

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
        log.exception(
            "rollback_staged failed - manual recovery from %s may be needed", staged_dir
        )


def extract_full_backup(data_dir: Path, zip_path: Path) -> None:
    """検証済みのバックアップzipを data_dir へ展開する (呼び出し前に stage_aside 済みであること)。"""
    with zipfile.ZipFile(zip_path) as zf:
        validate_backup_zip(zf, data_dir)
        for info in zf.infolist():
            if info.is_dir() or info.filename == MANIFEST_NAME:
                continue
            # allowlist は validate 済み。extract は data_dir 直下へ
            zf.extract(info, data_dir)


def recover_incomplete_restore(data_dir: Path) -> str | None:
    """起動時に呼ぶ。中断された復元の痕跡があれば最新 before_restore_* を書き戻す。

    戻り値: 復旧した場合はその説明文字列、不要なら None。
    open_db の前に呼ぶこと — 空の photofinder.db を新規作成してしまうのを防ぐ。
    """
    bdir = backup_dir(data_dir)
    marker = _restore_marker_path(data_dir)
    stages = sorted(bdir.glob("before_restore_*"), reverse=True) if bdir.is_dir() else []
    live_db = data_dir / DB_ARCNAME
    needs = marker.exists() or (not live_db.exists() and bool(stages))
    if not needs:
        return None
    if not stages:
        marker.unlink(missing_ok=True)
        log.error(
            "incomplete restore marker present but no before_restore_* found under %s",
            bdir,
        )
        return "incomplete restore marker cleared; no staged data to roll back"
    staged = stages[0]
    log.warning(
        "incomplete restore detected (marker=%s, live_db_exists=%s) - "
        "rolling back from %s",
        marker.exists(), live_db.exists(), staged,
    )
    rollback_staged(data_dir, staged)
    _clear_restore_marker(data_dir)
    # 余分な古いステージがあれば残してよい (次回復元で掃除)
    return f"rolled back incomplete restore from {staged.name}"


def cleanup_legacy_db_snapshots(data_dir: Path) -> int:
    """旧・週次 VACUUM スナップショット (backup/photofinder-YYYYMMDD-*.db) を削除。

    フルバックアップ移行後は不要で容量だけ食う。before_restore_* は触らない。
    """
    bdir = backup_dir(data_dir)
    if not bdir.is_dir():
        return 0
    n = 0
    for p in bdir.glob("photofinder-*.db"):
        try:
            p.unlink()
            n += 1
        except OSError:
            log.exception("failed to remove legacy snapshot %s", p)
    return n


def run_full_restore(data_dir: Path, zip_path: Path, db) -> None:
    """バックグラウンドスレッドから呼ぶ想定。結果は STATUS に反映する。

    db は .execute() を持つ接続 (main.py の LockedConnection)。生きたWALの
    内容を確定させてから退避するため (空でないWALを残したままphotofinder.db
    本体だけ退避すると、退避先を後で使う際に古い内容が混ざりうる)。

    呼び出し前に try_acquire("restore") でロックを取得済みであること。os._exit は
    呼ばない (プロセス管理は main.py 側の責務 — STATUS.phase=="done" を見て
    再起動をスケジュールする)。
    """
    # try_acquire 時点で running=True / phase=uploading 済み。ここから本処理
    STATUS.phase = "validating"
    STATUS.error = None
    staged: Path | None = None
    try:
        with zipfile.ZipFile(zip_path) as zf:
            validate_backup_zip(zf, data_dir)
            needed = estimate_extract_bytes(zf)
        check_free_space(data_dir, needed)

        STATUS.phase = "staging"
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # マーカーを先に書いてから rename — クラッシュ時は起動時 recover が拾う
        # staged 名はまだ決まっていないのでプレースホルダを書き、stage 後に更新
        _write_restore_marker(data_dir, "pending")
        try:
            staged = stage_aside_current_data(data_dir)
            _write_restore_marker(data_dir, staged.name)
        except Exception:
            _clear_restore_marker(data_dir)
            raise

        STATUS.phase = "extracting"
        try:
            extract_full_backup(data_dir, zip_path)
        except Exception:
            rollback_staged(data_dir, staged)
            _clear_restore_marker(data_dir)
            raise

        _clear_restore_marker(data_dir)
        STATUS.phase = "done"
    except Exception as e:
        log.exception("full restore failed")
        STATUS.error = str(e)
        STATUS.phase = "error"
    finally:
        STATUS.running = False
        release()
