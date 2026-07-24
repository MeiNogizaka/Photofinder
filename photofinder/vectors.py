"""FAISS ベクトルストア。vector id = photos.id。

docs/data-schema.md の FAISS 構成に対応:
  - IndexIDMap2(IndexHNSWFlat(ml.DIM, M=32, 内積))
  - tmp 書き → rename でアトミック保存
  - 削除は tombstone (photos.deleted=1) 方式。検索側で除外する

埋め込みモデルの変更 (ml.DIM 変更) で既存ファイルの次元と食い違う場合、
異次元ベクトルを add_with_ids しようとして FAISS が例外を投げる。
scanner.ML_VERSION のバックフィルで再エンコードが走ってもこの索引自体は
古い次元のまま残ってしまうため、__init__ で次元不一致を検知したら空の
索引を作り直す (中身は破棄される。scanner の ML_VERSION バックフィルが
全写真を再エンコードして埋め直す前提)。

**HNSWパラメータ**: FAISSの既定 (efConstruction=40, efSearch=16) は個人
コレクション規模 (数万〜数十万枚) のリコールを狙うには低すぎ、未調整の
まま放置されていた。efConstruction=200は構築時コストと引き換えにグラフ
品質を大きく上げる (100〜500が実用域、500を超えると効果が頭打ちになる
のが一般的な指針)。efSearchはクエリのkに対して動的に決める
(`max(HNSW_EF_SEARCH_MIN, min(HNSW_EF_SEARCH_MAX, k*2))`) — main.pyの
top-k動的拡張 (フィルタ絞り込み時にk=200〜2000へ広がる) に合わせて
毎回自動調整するため、静的な固定値だと小kクエリで過剰/大kクエリで
不足のどちらかになってしまう問題を避けられる。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

import faiss
import numpy as np

from .ml import DIM

log = logging.getLogger("photofinder.vectors")

HNSW_M = 32
HNSW_EF_CONSTRUCTION = 200
HNSW_EF_SEARCH_MIN = 128
HNSW_EF_SEARCH_MAX = 4000


def _new_hnsw_index(dim: int) -> faiss.IndexIDMap2:
    base = faiss.IndexHNSWFlat(dim, HNSW_M, faiss.METRIC_INNER_PRODUCT)
    base.hnsw.efConstruction = HNSW_EF_CONSTRUCTION
    return faiss.IndexIDMap2(base)


def _read_index(path: Path) -> faiss.Index:
    """faiss.read_index の Unicode パス安全版。

    faiss の C++ 側 (FileIOReader/Writer) は Windows で非ASCII文字を含む
    パスを正しく開けず "Illegal byte sequence" で失敗することを実機で確認した
    (日本語Windowsの既定の「デスクトップ」フォルダ配下に配置したケース、
    2026-07-14)。ファイルは Python 組み込みの open() で開き (Windows の
    ワイド文字API経由でUnicode安全)、faiss にはコールバック経由でバイト列
    だけ渡すことでこの制限を回避する。
    """
    with open(path, "rb") as f:
        return faiss.read_index(faiss.PyCallbackIOReader(f.read))


def _write_index(index: faiss.Index, path: Path) -> None:
    """faiss.write_index の Unicode パス安全版 (_read_index 参照)。"""
    with open(path, "wb") as f:
        writer = faiss.PyCallbackIOWriter(f.write)
        try:
            faiss.write_index(index, writer)
        finally:
            # PyCallbackIOWriter はデストラクタでバッファを f.write へフラッシュする。
            # f が閉じる前に必ず破棄すること (finally なのは、write_index が失敗した
            # 場合でも GC 任せにすると閉じた f への書き込みで元の例外を覆い隠すため)
            del writer


class VectorStore:
    def __init__(self, data_dir: Path, dim: int = DIM):
        self.path = data_dir / "vectors.faiss"
        self.dim = dim
        self._lock = threading.Lock()
        if self.path.exists():
            self.index = _read_index(self.path)
            if self.index.d != dim:
                log.warning(
                    "vectors.faiss dim mismatch (file=%d, expected=%d); "
                    "discarding and rebuilding empty (ML_VERSION backfill will re-populate)",
                    self.index.d, dim)
                self.index = _new_hnsw_index(dim)
        else:
            self.index = _new_hnsw_index(dim)

    @property
    def count(self) -> int:
        return self.index.ntotal

    def add(self, ids: list[int], vecs: np.ndarray) -> None:
        # HNSW は remove_ids 非対応。写真の内容変更で同一 id が重複し得るが、
        # 検索側で dedupe する（古いベクトルは再構築時に消える）。
        with self._lock:
            self.index.add_with_ids(
                np.ascontiguousarray(vecs, dtype=np.float32),
                np.asarray(ids, dtype=np.int64))

    def get_vector(self, photo_id: int) -> np.ndarray | None:
        """指定 photo_id の埋め込みを取り出す (未ベクトル化・未反映なら None)。"""
        with self._lock:
            try:
                return self.index.reconstruct(photo_id)
            except Exception:
                return None

    def search(self, vec: np.ndarray, k: int) -> list[tuple[int, float]]:
        if self.index.ntotal == 0:
            return []
        with self._lock:
            # efSearchはリコール/レイテンシのトレードオフ。kが動的に広がっても
            # (main.pyのtop-k拡張) 追従するよう、呼び出しのたびkから決め直す。
            # IndexIDMap2.index は素の faiss.Index (SWIG基底クラス) として返ってくる
            # ため、.hnsw を触るには downcast_index で具象型(IndexHNSWFlat)に戻す必要が
            # ある(実機で AttributeError: 'Index' object has no attribute 'hnsw' を確認済み)
            faiss.downcast_index(self.index.index).hnsw.efSearch = max(
                HNSW_EF_SEARCH_MIN, min(HNSW_EF_SEARCH_MAX, k * 2))
            scores, ids = self.index.search(
                np.ascontiguousarray(vec, dtype=np.float32).reshape(1, -1),
                min(k, self.index.ntotal))
        seen: set[int] = set()
        out: list[tuple[int, float]] = []
        for i, s in zip(ids[0], scores[0]):  # スコア降順なので先勝ちで dedupe
            if i != -1 and int(i) not in seen:
                seen.add(int(i))
                out.append((int(i), float(s)))
        return out

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        """tmp 書き → rename のアトミック保存。呼び出し側が self._lock を保持していること。"""
        tmp = self.path.with_suffix(".tmp")
        _write_index(self.index, tmp)
        tmp.replace(self.path)

    def rebuild(self, valid_ids: set[int]) -> dict:
        """索引を作り直して重複ベクトルと不要 id を除去する。

        HNSW は remove 非対応なので、再インデックスのたびに同一 photo_id の
        ベクトルが積み重なる (検索時 dedupe で実害はないが索引が肥大化)。
        既存ベクトルを reconstruct で取り出し、photo_id ごとに最新1本だけ
        (IndexIDMap2 の id→位置マップは後勝ちのため reconstruct が最新を返す) 、
        かつ valid_ids に含まれる id (= 未削除の写真) のみで作り直す。
        画像の再エンコード不要。tmp→rename でアトミック保存。
        """
        with self._lock:
            before = self.index.ntotal
            if before == 0:
                return {"before": 0, "after": 0, "removed": 0}
            idmap = faiss.vector_to_array(self.index.id_map).astype("int64")
            keep = sorted({int(x) for x in idmap} & valid_ids)
            new = _new_hnsw_index(self.dim)
            if keep:
                vecs = np.stack([self.index.reconstruct(i) for i in keep])
                new.add_with_ids(
                    np.ascontiguousarray(vecs, dtype=np.float32),
                    np.asarray(keep, dtype=np.int64))
            self.index = new
            self._save_locked()
            return {"before": before, "after": new.ntotal,
                    "removed": before - new.ntotal}

    def flush_pending(self, db: sqlite3.Connection) -> int:
        """faiss_pending の add をインデックスへ反映して保存。反映件数を返す。"""
        rows = db.execute(
            "SELECT photo_id, op, vector FROM faiss_pending").fetchall()
        if not rows:
            return 0
        adds = [(r["photo_id"], r["vector"]) for r in rows
                if r["op"] == "add" and r["vector"]]
        if adds:
            vecs = np.stack([
                np.frombuffer(v, dtype=np.float16).astype(np.float32)
                for _, v in adds])
            self.add([i for i, _ in adds], vecs)
        # remove は tombstone (photos.deleted) で検索時除外されるため、
        # ここではキューを消化するのみ。削除率が上がったら再構築で物理反映する。
        self.save()
        db.execute("DELETE FROM faiss_pending")
        db.commit()
        return len(rows)
