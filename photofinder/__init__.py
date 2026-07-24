"""PhotoFinder2 — ローカル写真検索アプリ (Docker配布版)。

app2/photofinder (0.6.1) からのフォーク。Windows exe配布を廃止しDocker専用
(CPU/CUDAの2バリアント) に移行、RAW対応、FAISS/RRFチューニング、人手タグの
データセット書き出し機能を追加。バージョンはここが唯一の定義元
(main.py の FastAPI titleにも渡る)。フォーク時点でバージョンを0.1.0から
再出発させている (app2/photofinderの0.x系とは独立した番台)。

0.1.0: photofinder2として新規フォーク。Docker専用配布 (CPU/CUDA)、RAW対応
       (rawpy埋め込みプレビュー抽出)、FAISS HNSWパラメータ調整+top-k動的拡張、
       人手タグ付け済み写真のデータセット書き出し (/api/export/dataset)、
       透かし書き出しのCJKフォント修正 (Linux/Noto Sans・Serif JP)
"""

__version__ = "0.1.0"
