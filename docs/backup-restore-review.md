# フルバックアップ／復元 コードレビューと対応記録

- **対象コミット (初回実装)**: `979a4e1` — Replace weekly DB-only snapshot with full data backup/restore  
- **レビュー日**: 2026-08-07  
- **フォローアップ**: 0.3.1（本ドキュメント記載の指摘への修正）  
- **リポジトリ**: https://github.com/MeiNogizaka/Photofinder  
- **前提**: 当初は個人運用想定。将来の公開を見据え、同時実行・クラッシュ復旧・入力検証を強化した。

---

## 1. 総評

週次の DB のみ `VACUUM INTO` スナップショットをやめ、データディレクトリ全体（DB・FAISS・サムネ／プレビュー・poi.db 等）を UI から zip で出し入れする設計は妥当である。

良い点:

- DB は専用接続の `VACUUM main INTO` で一貫スナップショット
- ライブデータに触る前に zip 検証
- rename 退避 + 展開失敗時のロールバック
- 成功時はプロセス終了 → Docker `restart` で FAISS を再読込（ホットスワップしない）

初回実装時点の主なギャップは、**復元とスキャン等の同時実行**、**途中クラッシュ後の空 DB 起動**、**ステージング途中失敗の未ロールバック**、**UI が復元中にボタンを再有効化する**ことだった。0.3.1 で対応済み。

---

## 2. 指摘一覧と対応状況

| # | 深刻度 | 内容 | 対応 |
|---|--------|------|------|
| 1 | bug | バックアップ／復元中にスキャン等が開始でき、`stage_aside` 後に `open_db` が空 DB を作りうる | **修正済** — 共通ロック `try_acquire` / `is_busy()`。スキャン・ベクトル再構築・アーカイブ取込・フォルダ追加を拒否 |
| 2 | bug | 復元途中クラッシュ後、起動時に空の `photofinder.db` が新規作成され、実データが `before_restore_*` に隠れる | **修正済** — `RESTORE_IN_PROGRESS` マーカー + `recover_incomplete_restore()` を `open_db` より前に実行 |
| 3 | bug | `stage_aside_current_data` 途中失敗で部分 rename のまま、かつ旧世代を先に削除していた | **修正済** — 部分ロールバック。旧 `before_restore_*` は新ステージ成功後に削除 |
| 4 | bug | 2 秒ポーリングがスキャン状態だけで復元ボタンを再有効化 | **修正済** — `restoreInProgress` + `backup_busy` |
| 5 | suggestion | 空き容量の説明不足（展開中は旧+新ツリー） | **修正済** — 展開前 `check_free_space`、UI／docs に記載 |
| 6 | suggestion | アップロードサイズ上限なし | **修正済** — 既定 32GiB（`PHOTOFINDER_BACKUP_MAX_UPLOAD_BYTES`） |
| 7 | suggestion | `format_version` 未検証・メンバー allowlist なし | **修正済** — バージョン強制 + DB／オプション／`thumbs|previews` のみ |
| 8 | suggestion | 復元 UI の 409／非 JSON エラー表示が弱い | **修正済** — download 側と同様の `try/catch` と `detail` 表示 |
| 9 | suggestion | STATUS リセットが BG スレッド任せで一瞬古い `error` を拾いうる | **修正済** — `try_acquire("restore")` 時に `phase=uploading` |
| 10 | nit | 旧週次 `photofinder-*.db` スナップショットが残る | **修正済** — 起動時 `cleanup_legacy_db_snapshots` |
| 11 | nit | UI に再起動・空き容量・手動復旧パスの説明が薄い | **修正済** — 設定画面の説明文を拡充 |

---

## 3. 設計メモ（公開時に押さえる点）

### 3.1 なぜプロセス再起動が必要か

`VectorStore` は起動時に一度だけ `vectors.faiss` をメモリへ載せる。ディスク上のファイルだけ差し替えても検索結果は古いままになる。復元成功後は `os._exit(0)` → Docker の `restart: unless-stopped` で開き直す。

### 3.2 ディスク使用量

| 段階 | 追加容量の目安 |
|------|----------------|
| rename 退避 | 同一 FS なら追加ほぼ不要 |
| 展開中 | 旧ツリー（退避済み）+ 新ツリー + アップロード zip |
| ピーク | おおよそ **非圧縮バックアップサイズ分の空き** が必要 |

### 3.3 認証

アプリ本体に認証はない。既定の `127.0.0.1:8686` バインドを前提とする。LAN／インターネット公開時はリバースプロキシ等で保護すること（バックアップ zip はインデックス一式を含む）。

### 3.4 環境変数

| 変数 | 既定 | 意味 |
|------|------|------|
| `PHOTOFINDER_BACKUP_MAX_UPLOAD_BYTES` | `34359738368` (32GiB) | 復元 zip のアップロード上限。`0` 以下で無制限 |

---

## 4. 主な変更ファイル (0.3.1)

- `photofinder/backup.py` — 排他・検証強化・マーカー復旧・空き容量・ステージング安全化
- `photofinder/main.py` — 起動時 recovery、API ゲート、アップロード上限、`index/status` に `backup_busy`
- `photofinder/static/index.html` — 復元中 UI 状態、エラー処理、説明文
- `photofinder/__init__.py` — バージョン 0.3.1
- `docs/api-spec.md`, `docs/docker.md`, `CLAUDE.md` — 仕様追従

---

## 5. 推奨の手動確認手順

1. 設定 → バックアップをダウンロード → zip が得られ、`backup_manifest.json` と `photofinder.db` が含まれること  
2. データを少し変更 → 同じ zip で復元 → 再起動後に変更前の状態に戻ること  
3. スキャン中に復元／バックアップが 409 または UI 上無効になること  
4. 壊した zip（manifest 欠落・`format_version` 改変・`../` パス）が拒否され、ライブデータが無傷なこと  
5. （任意）復元中にプロセスを強制終了 → 再起動後に `before_restore_*` から書き戻され、空 DB で起動しないこと  

---

## 6. 残課題（任意・将来）

- 進捗パーセント表示（現状は phase のみ）
- バックアップ作成の非同期化（巨大ライブラリで HTTP タイムアウトしうる）
- 増分バックアップ
- 公開時のリバースプロキシ＋認証の推奨構成を README にテンプレ化
