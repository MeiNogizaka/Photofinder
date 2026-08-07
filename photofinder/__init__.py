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
0.3.0: 週次DBスナップショット (VACUUM INTO・3世代・自動+手動) を廃止し、
       フルデータバックアップ/復元 (POST /api/backup/full, POST
       /api/backup/full-restore) に置き換え。DBだけでなくFAISS索引・
       サムネ/プレビュー・poi.db・種名/色バンクキャッシュを含むデータ
       ディレクトリ全体をzipで往復できる。復元前にライブデータを
       data/backup/before_restore_<timestamp>/ へrenameで退避 (直前
       1世代のみ保持)、失敗時はロールバックしプロセスは再起動しない。
       復元成功時は旧DB単体復元と同じ「プロセス終了→コンテナのrestart
       policyで再起動」パターンを踏襲 (vectors.faissが起動時一度きり
       メモリへ読み込まれ、稼働中の差し替えが反映されないため)。設定の
       backup_auto を廃止 (schema v7、既存DBの当該行は起動時マイグレーション
       で削除)
0.3.1: フルバックアップ/復元の安全対策。バックアップ・復元の排他ロックを
       スキャン/ベクトル再構築/アーカイブ取込と共有し、復元中の空DB作成
       競合を防止。RESTORE_IN_PROGRESS マーカーと起動時
       recover_incomplete_restore で途中クラッシュ後の自動復旧。ステージング
       途中失敗の部分ロールバック、format_version/メンバーallowlist検証、
       展開前の空き容量チェック、アップロードサイズ上限
       (PHOTOFINDER_BACKUP_MAX_UPLOAD_BYTES、既定32GiB)、UIの復元中ボタン
       再有効化防止とエラー表示改善、旧週次スナップショットの起動時掃除
"""

__version__ = "0.3.1"
