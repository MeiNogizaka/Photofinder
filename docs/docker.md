# Docker配布

photofinderはDocker専用配布 (Windows exeビルドは廃止)。CPU/CUDAの2バリアントを
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
| `/app/data` | SQLite DB・FAISS索引・サムネ/プレビュー・バックアップ・poi.db | named volume (`photofinder_data`) |
| `/app/models` | SigLIP2/YOLOv8x/OCRのONNXモデル | named volume (`photofinder_models`) |
| `/photos` | 写真ライブラリ (read-only) | bind mount (ホスト側フォルダ) |

設定画面の「バックアップ / 復元」から、DB・FAISS索引・サムネ/プレビュー・poi.db等
`data/`全体をzipでダウンロード/復元できる (`POST /api/backup/full`/`full-restore`)。
復元前のライブデータの退避先 (直前1世代のみ) も同じ`photofinder_data`ボリューム内
`data/backup/before_restore_<timestamp>/`に作られる (docs/api-spec.md参照)。

### WSL2でのパフォーマンスの注意

WSL2上でコンテナを動かし、`PHOTO_LIBRARY_PATH`にWindows側フォルダ
(`/mnt/c/Users/...`等) を指定すると、WSL2からNTFSへのアクセスは小さいファイルを
大量に読み書きする処理 (差分スキャンのxxHash計算・サムネ/プレビュー生成など) で
体感できるほど遅くなることが知られている (WSL2のよく知られた制約で、
photofinder固有の問題ではない)。写真枚数が多いライブラリでは:

- 可能なら写真そのものをWSL2側のネイティブファイルシステム (例: `~/Pictures`) に
  置き、`PHOTO_LIBRARY_PATH`もそちらを指す
- Windows側に置かざるを得ない場合は、初回スキャンが`/mnt/c/...`経由のI/O待ちで
  遅くなる前提で計画する (`GET /api/index/status`の`rate_per_min`で実測できる)

### 複数のホストフォルダを登録したい場合

`docker-compose.yml`のbind mountは既定で1本 (`/photos`) だけなので、UIから
登録できるのはその配下のみ。物理的に分かれた複数フォルダ (例: Windows側の
`Pictures`とは別にDドライブの`Photos`も対象にしたい) を扱いたい場合は、
`docker-compose.yml`の該当サービスにマウントをもう1行追加する:

```yaml
    volumes:
      - photofinder_data:/app/data
      - photofinder_models:/app/models
      - ${PHOTO_LIBRARY_PATH}:/photos:ro
      - ${PHOTO_LIBRARY_PATH2}:/photos2:ro   # 追加
```

`.env`に`PHOTO_LIBRARY_PATH2`を追加した上で`docker compose up`し直せば、UIから
`/photos`と`/photos2`の両方をルートとして登録できるようになる。

## アップデート手順

**`data`/`models`はDocker named volumeであり、リポジトリのファイルではない**
(上記「ボリューム」参照)。そのためソースコードの更新それ自体は既存データに
一切触れない。DB・サムネ・FAISS索引・POIデータ・バックアップは、ソース更新の
方法(git pull / zip展開のどちらでも)に関係なく保持される。

### git pullで更新する場合

```bash
git pull
docker compose --profile cpu build      # cudaなら --profile cuda
docker compose --profile cpu up -d      # コンテナを作り直す。volumeはそのまま引き継ぐ
```

### zipをダウンロードして更新する場合(gitを使わない場合)

1. 新しいソースのzipを取得し、別の一時フォルダに展開する
2. 今使っているフォルダから `.env` をコピーする(zipには含まれない — `.env`は
   `.gitignore`対象でリポジトリ自体に含まれないため、上書きの心配は無い)
3. 今のコンテナを停止する: `docker compose --profile cpu down`
   (**`-v` を付けないこと** — `-v`はvolumeごと削除してしまう)
4. 展開した新しいフォルダに移動し、`docker compose --profile cpu build && docker
   compose --profile cpu up -d` を実行する

**注意点(実機で確認済みの落とし穴)**: docker composeは既定でプロジェクト名
(≒volume名の接頭辞)を**カレントディレクトリ名**から決める。zipの展開先フォルダ名
が元のフォルダ名と異なる(例: `photofinder-0.2.0/`のように展開される)と、
別プロジェクト扱いになり既存の`photofinder_data`/`photofinder_models`
volumeを見失う — データが消えるわけではない(volume自体はDocker上に残り続ける)
が、新しいコンテナは空のvolumeで起動してしまい、一見データが消えたように見える。
これを避けるため`docker-compose.yml`の先頭に`name: photofinder`を明示している
(フォルダ名に依存しない)。**この行を削除・変更しないこと。**
万一(古いフォルダ名依存のバージョンで運用していた等の理由で)新しいvolumeが
作られてしまった場合は、`docker volume ls`で古い方(例:
`<旧フォルダ名>_photofinder_data`のような名前)が残っているか確認し、
新しく作られた空のvolumeを`docker compose down -v`で消してから、
`docker-compose.yml`の`volumes:`定義に`external: true`と`name:`を指定して
古いvolumeを明示的に指すようにすれば復旧できる:
```yaml
volumes:
  photofinder_data:
    external: true
    name: <旧フォルダ名>_photofinder_data
  photofinder_models:
    external: true
    name: <旧フォルダ名>_photofinder_models
```

### モデル・スキーマの扱い

モデル(SigLIP2/YOLOv8x/OCR)を差し替える更新でない限り、`model-fetch`の
再実行は不要。DBスキーマの変更は`photofinder/db.py`の`_migrate()`が
`schema_meta.schema_version`を見て起動時に自動で追記型のマイグレーションを
行うため、手動でのDB操作は基本的に不要(既存行を壊さない設計)。念のため、
更新前に設定画面の「バックアップ / 復元」から手動でフルバックアップを
ダウンロードしておくと安全。

## 環境移行(別マシンへの引っ越し)

**基本の方法: アプリ内蔵のフルバックアップ/復元を使う**(設定画面の
「バックアップ / 復元」、`POST /api/backup/full`/`full-restore`)。旧環境で
バックアップzipをダウンロードし、新環境で(空の状態で)起動したコンテナへ
そのzipをアップロードして復元するだけで、DB・タグ・サムネ・FAISS索引・
poi.dbが全部そのまま移り、**新環境での再スキャンは不要**になる。docker
volume操作は一切不要。復元は数百MB〜数GB規模のブラウザアップロードになるため、
低速/不安定な回線ではなくローカルネットワーク越しに行うことを推奨する。

**フォールバック: docker volumeを「一時コンテナ+tar」で丸ごと移す方法**
(実機で動作確認済み)。ライブラリが非常に大きく、ブラウザ経由のzip
アップロードより生の`tar`/`scp`の方が確実な場合に使う。volume名は
`docker compose --profile cpu config --format json`で確認できる
(`name: photofinder`+volumeキー`photofinder_data`から実際には
`photofinder_photofinder_data`になる):

```bash
docker compose --profile cpu down   # -v は付けない
docker run --rm \
  -v photofinder_photofinder_data:/from \
  -v "$(pwd)":/backup \
  alpine tar czf /backup/pf2_data_backup.tar.gz -C /from .
```

`pf2_data_backup.tar.gz`をUSBメモリ・scp等で新環境に転送し、**新環境で復元**:

```bash
docker volume create photofinder_photofinder_data
docker run --rm \
  -v photofinder_photofinder_data:/to \
  -v "$(pwd)":/backup \
  alpine tar xzf /backup/pf2_data_backup.tar.gz -C /to
docker compose --profile cpu up -d
```

**`models`は移行しなくてよい**。モデル自体(SigLIP2/YOLOv8x/OCR、計約1.5GB)が
変わっていなければ、大きなtarを転送するより新環境で`docker compose --profile
setup run --rm model-fetch`をやり直す方が簡単(新環境にも元々ネット接続は
必要なので追加の要件にはならない)。

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
