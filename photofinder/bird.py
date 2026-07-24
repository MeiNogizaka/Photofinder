"""野鳥種名推定 (M5): 専用分類器を学習せず、既存の汎用モデル (SigLIP) をゼロショット流用。

方式:
  YOLO が bird を検出 → bbox を 1.2 倍にパディングして切り出し
  → SigLIP 画像埋め込み vs 種名テキスト埋め込み (species_ja.BIRDS) のコサイン類似度

なぜ専用分類器 (Kaggle BIRDS 525 等) を使わないか:
  既製の 525 種分類器はカワセミ (Alcedo atthis) 等の日本の普通種をクラスに持たず、
  近縁の別種 (Malachite Kingfisher) を高信頼度で誤答する。クラス集合を編集できないため
  日本の野鳥用途では原理的に正解できない。SigLIP ゼロショットなら候補集合を
  species_ja.py で自由に定義でき、モデルの追加ダウンロードも不要。

精度の実測 (実写7枚, 種リスト約100種, SigLIP2 so400m 移行後 2026-07-19):
  top-3 正解率 6/7、top-1 正解率 3/7 — SigLIP (base) 時代の実測 (top-3 4/4, top-1 2/4)
  と大差なし。近縁種 (カワセミ/アカショウビン、コサギ/チュウサギ 等) の混同は
  モデルを大きくしても解消しない、zero-shot 埋め込みそのものの限界と見られる。
  → 誤った種名タグは検索を汚染するため、**マージンが十分大きいときだけ自動確定**し、
    それ以外は top-3 を「候補」として UI に出し、ユーザが1クリックで確定する。
  閾値は下記 MIN_SIM/MIN_MARGIN で較正 (この7枚では誤自動確定 0 件)。ただし
  n=7 はまだ小さく、旧モデル較正時 (実写5枚) 同様サンプル不足の粗い較正でしかない。
  実運用でご自身の写真に対して誤確定が出ないか、引き続き様子見が必要。

信頼度の扱い:
  softmax 確率ではなく top1 の絶対類似度と top1-top2 マージンで判定する。
  候補数が増えると softmax は必ず下がり、閾値が候補数に依存してしまうため。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading

import numpy as np
from PIL import Image

from .paths import data_dir
from .species_ja import BIRDS

log = logging.getLogger("photofinder.bird")

CACHE_PATH = data_dir() / "species_bank.npz"
TOPK = 3

# 自動確定の閾値: 両方を満たしたときだけ species_ja を確定する。
# 誤タグ (検索汚染) より取りこぼしを許容する側に倒す = 適合率優先。
# SigLIP2 移行後の実測 (2026-07-19, 実写7枚):
#   スズメ margin=0.0170 → 確定 (正解)
#   カワセミ margin=0.0139, コサギ margin=0.0102 → 旧閾値 (0.010) だと確定してしまい
#   どちらも誤答 (近縁種と混同) だったため MIN_MARGIN を 0.015 に引き上げた。
#   アオサギ margin=0.0109 (正解だが MIN_SIM 未達で非確定) はそのまま許容。
MIN_SIM = 0.10       # top1 の絶対コサイン類似度
MIN_MARGIN = 0.015   # top1 - top2 のマージン (近縁種と識別が付いているか)

TEMPLATES = (
    "{ja}の写真",
    "野鳥の{ja}",
    "a photo of a {en}, a species of bird",
    "{sci}, a bird",
)


class BirdClassifier:
    """SigLIP テキスト埋め込みバンクを1度だけ構築してキャッシュする。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._bank: np.ndarray | None = None

    @property
    def available(self) -> bool:
        from . import ml
        return ml.runtime.available and bool(BIRDS)

    @staticmethod
    def _species_hash() -> str:
        from . import ml
        # ml.DIM を混ぜることで埋め込みモデル変更時 (次元が変わる) もキャッシュを
        # 無効化する。種リスト/テンプレートが同じでも古い次元のバンクを読み込むと
        # classify_vec() の `bank @ crop_vec` が次元不一致で例外になるため。
        blob = json.dumps(BIRDS, ensure_ascii=False).encode()
        return hashlib.sha256(
            blob + str(TEMPLATES).encode() + str(ml.DIM).encode()).hexdigest()[:16]

    def _load_bank(self) -> np.ndarray:
        """種名テキストのバンク (n_species, ml.DIM)。種リスト/モデル変更時は自動で作り直す。"""
        with self._lock:
            if self._bank is not None:
                return self._bank
            h = self._species_hash()
            if CACHE_PATH.exists():
                cached = np.load(CACHE_PATH)
                if str(cached.get("hash")) == h:
                    self._bank = cached["bank"]
                    return self._bank

            from . import ml
            log.info("building species text bank (%d species)…", len(BIRDS))
            vecs = []
            for ja, en, sci in BIRDS:
                embs = [ml.runtime.siglip_text(t.format(ja=ja, en=en, sci=sci))
                        for t in TEMPLATES]
                v = np.mean(embs, axis=0)
                vecs.append(v / (np.linalg.norm(v) or 1.0))
            self._bank = np.stack(vecs).astype(np.float32)
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            np.savez(CACHE_PATH, bank=self._bank, hash=h)
            return self._bank

    def classify(self, crop: Image.Image) -> dict | None:
        """画像から直接 (テスト用)。本番は classify_vec に埋め込みを渡して再利用する。"""
        from . import ml
        return self.classify_vec(ml.runtime.siglip_image(crop))

    def classify_vec(self, crop_vec: np.ndarray) -> dict | None:
        """SigLIP 画像埋め込み (bird crop) → {species_ja, species_sci, conf, topk_json}。
        判定が付かないときは species_ja=None (「野鳥」タグのみに留める)。"""
        bank = self._load_bank()
        sims = bank @ crop_vec

        order = sims.argsort()[::-1]
        top = order[:TOPK]
        margin = float(sims[order[0]] - sims[order[1]]) if len(order) > 1 else 1.0
        best_sim = float(sims[order[0]])
        confident = best_sim >= MIN_SIM and margin >= MIN_MARGIN

        topk = [(BIRDS[i][0], round(float(sims[i]), 4)) for i in top]
        return {
            "species_ja": BIRDS[order[0]][0] if confident else None,
            "species_sci": BIRDS[order[0]][2],
            "conf": round(best_sim, 4),
            "margin": round(margin, 4),
            "topk_json": json.dumps(topk, ensure_ascii=False),
        }


classifier = BirdClassifier()
