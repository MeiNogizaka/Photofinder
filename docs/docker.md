# Docker配布

photofinder2はDocker専用配布 (Windows exeビルドは廃止)。CPU/CUDAの2バリアントを
1本の`Dockerfile`からビルド引数`VARIANT`で作り分ける。

## クイックスタート

```bash
cp .env.example .env
# .env を編集し PHOTO_LIBRARY_PATH をホスト側の写真フォルダに設定

# モデル取得 (初回のみ、SigLIP2/YOLOv8x/OCR 計約1.5GB)
docker compose --profile setup run --rm model-fetch

# 起動 (CPU環境)
docker compose --profile cpu up -d
# 起動 (NVIDIA GPU環境。要 nvidia-container-toolkit)
docker compose --profile cuda up -d
```

`http://127.0.0.1:8686` をブラウザで開く。UI右下の「＋ フォルダを追加」で走査
フォルダを登録する際は、**コンテナ内パス** `/photos/...` を入力すること
(`docker-compose.yml`のマウント設定 `PHOTO_LIBRARY_PATH:/photos:ro` 参照)。
ホスト側の実パスを入力しても、コンテナから見えないため走査できない。

## なぜモデル取得だけ別ステップなのか

`tools/download_models.py`はネットワーク経由でHugging Faceからモデルを取得し、
YOLOv8xのONNXエクスポートには一時的に`ultralytics`(torch同梱、重い)が必要になる。
これをアプリイメージの`docker build`に含めてしまうと、アプリコードを1行直しただけの
再ビルドでも毎回1.5GB前後を再取得することになり、かつCPU/CUDA両バリアントのイメージに
モデルが重複して焼き込まれる。そのためモデル取得はアプリイメージのビルド/起動
ライフサイクルから完全に切り離し、`models`用named volumeに一度だけ保存する
設計にしている (`Dockerfile`の`model-fetch`ステージ、`docker-compose.yml`の
`model-fetch`サービス参照)。

**重要**: `model-fetch`はイメージの`docker build`時ではなく、`docker compose
--profile setup run --rm model-fetch`で**コンテナを実行**して初めてボリュームに
書き込まれる。`docker build`はボリュームをマウントしないため、Dockerfile側で
モデル取得を`RUN`にしてしまうとそのビルドステージのイメージ層に焼き込まれるだけで
named volumeには何も残らない。

## CPU/CUDAをイメージで分けている理由

1つのCUDA対応ファットイメージに寄せる案(単一イメージ+実行時フォールバック)も
検討したが、以下の理由でARGによるイメージ分岐を採用した:

- `requirements.txt`(CPU用onnxruntime)と`requirements-gpu.txt`
  (onnxruntime-gpu+nvidia-cudnn-cu13)は元々分離されており、そのままベース
  イメージ選択に対応させられる
- CPU環境のユーザーが不要なCUDAランタイム分のイメージサイズ/脆弱性パッチ対応を
  背負わずに済む
- 将来CUDAバージョンを上げる、他のGPUバックエンド(ROCm等)を足す、といった
  変化にARGの選択肢を増やすだけで対応できる

## ボリューム

| マウント先 | 内容 | 種別 |
|---|---|---|
| `/app/data` | SQLite DB・FAISS索引・サムネ/プレビュー・バックアップ・poi.db | named volume (`photofinder2_data`) |
| `/app/models` | SigLIP2/YOLOv8x/OCRのONNXモデル | named volume (`photofinder2_models`) |
| `/photos` | 写真ライブラリ (read-only) | bind mount (ホスト側フォルダ) |

`data/`のバックアップ (`VACUUM INTO`スナップショット、週次自動+手動) は
`photofinder2_data`ボリューム内`data/backup/`に作られる。FAISS索引・サムネは
DBから再構築可能なので、最悪DBだけ守れば復旧できる (README参照)。

## ネットワーク公開範囲 (認証機構は無い)

アプリ自体にトークン認証等は実装していない。**公開範囲は`docker-compose.yml`の
`ports:`マッピングがそのまま決める**:

- `"127.0.0.1:8686:8686"` (既定): このDockerホスト上からのみアクセス可能
- `"8686:8686"` または `"0.0.0.0:8686:8686"`: LAN/インターネットに公開される

LAN公開する場合は、リバースプロキシ側での認証やVPN経由でのアクセスに限定するなど、
利用者自身の責任でアクセス制御を行うこと。AGPL-3.0の「ネットワーク条項」(第13条、
README参照)は、ネットワーク経由で他者がアプリと対話できる状態になった時点で
ソース開示義務が生じる点にも留意すること。

## --workers 1 が必須な理由

`photofinder/main.py`はモジュールレベルで`db`(SQLite接続)と`vstore`(FAISS索引)の
シングルトンを保持する単一プロセス設計 (CLAUDE.md「Single process, three stores」)。
複数uvicornワーカーを立てると各ワーカーが独立にFAISS/SQLite状態を持ち、書き込みが
競合する。`Dockerfile`のCMDは常に`--workers 1`を指定している — 変更しないこと。

## トラブルシューティング

- `GET /api/index/status`のレスポンスに`in_docker: true`が含まれていれば、
  コンテナ実行がバックエンドから正しく検出できている
  (`エクスプローラで開く`系のUIボタンはコンテナでは自動的に非表示になる)。
- CUDAバリアントで`providers.siglip_text`等が`CPUExecutionProvider`のままの
  場合、ホストに`nvidia-container-toolkit`が入っているか、
  `docker compose --profile cuda up`が`deploy.reservations.devices`経由で
  GPUを正しく予約できているか確認する。CUDA関連パッケージ/ドライバが無い
  環境では例外にならず黙ってCPUにフォールバックするだけなので、
  ログにエラーが出ないまま気づかずCPU動作していることがある。
  実機確認済み: `nvidia-container-toolkit`を`sudo apt-get install`後、
  `sudo nvidia-ctk runtime configure --runtime=docker` → `sudo systemctl
  restart docker`でDockerにnvidiaランタイムを登録すれば、
  `docker compose --profile cuda up`だけで`CUDAExecutionProvider`が有効になる
  （WSL2 + RTX 3060で確認、`nvidia-smi`がホスト側で動くこと＝GPUパススルー
  自体は前提として必要）。
- `docker ps`で対象コンテナが`Restarting`を繰り返し、ログが
  `exists: yolov8x.onnx` / `done -> /app/models`のようなモデル取得スクリプトの
  出力ばかりの場合、`docker-compose.yml`の`photofinder-cpu`/`photofinder-cuda`
  サービスに`build.target: runtime`が抜けている（実機で再現・修正済みの不具合）。
  マルチステージDockerfileで`target`を省略すると**ファイル内最後のステージ**
  （このDockerfileでは`model-fetch`）が既定でビルドされてしまうため、意図せず
  model-fetchのイメージ/CMDでアプリコンテナが起動し、スクリプトが即終了→
  `restart: unless-stopped`で再起動…を繰り返す。エラーは一切出ないため気づき
  にくい。両サービスとも`target: runtime`を明示しているので、このDockerfileを
  ベースに新しいサービスを追加する場合も必ず`target:`を明示すること。
