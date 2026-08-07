"""PhotoFinder — ローカル写真検索アプリ (Docker配布版)。

app2/photofinder (0.6.1) からのフォーク。Windows exe配布を廃止しDocker専用
(CPU/CUDAの2バリアント) に移行、RAW対応、FAISS/RRFチューニング、人手タグの
データセット書き出し機能を追加。バージョンはここが唯一の定義元
(main.py の FastAPI titleにも渡る)。フォーク時点でバージョンを0.1.0から
再出発させている (app2/photofinderの0.x系とは独立した番台)。

0.1.0: photofinderとして新規フォーク。Docker専用配布 (CPU/CUDA)、RAW対応
       (rawpy埋め込みプレビュー抽出)、FAISS HNSWパラメータ調整+top-k動的拡張、
       人手タグ付け済み写真のデータセット書き出し (/api/export/dataset)、
       透かし書き出しのCJKフォント修正 (Linux/Noto Sans・Serif JP)
0.2.0: 書き出しダイアログを拡充 — 切り出し比率の自由入力、透かし文字サイズの
       調整、アルファチャンネル付き透かし画像のオーバーレイ、ウィンドウ幅に
       応じたダイアログ/プレビューの拡大表示。透かしフォントにM+ 1を追加
       (OFL-1.1)。書き出しの切り出しサイズ表示がプレビュー解像度 (1600px上限)
       に頭打ちになっていた表示バグを修正。docker-compose.ymlにCPU使用率制限
       (cpus)、コンテナのビルドターゲット指定漏れ (target: runtime。指定が無いと
       Dockerfile最後のステージ=model-fetchが誤ってビルドされる不具合) の修正、
       プロジェクト名固定 (name:) を追加。NAS(SMB)マウント手順をREADMEに追記。
       バックアップ復元機能を追加 (POST /api/backup/restore) — 復元前に自動で
       安全スナップショットを作成し、DBファイル差し替え後プロセスを終了して
       コンテナのrestart policyで再起動させる方式。付随してbackup.pyの
       スナップショットファイル名が秒精度までしか無く同一秒内の連続作成で
       衝突していた既存バグ (ミリ秒精度を追加) を修正
"""

__version__ = "0.2.0"
