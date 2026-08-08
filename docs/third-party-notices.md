# サードパーティ ライブラリ・モデル・データの権利/規約

本アプリが利用しているライブラリ・学習済みモデル・外部データについて、ライセンスと
留意事項をまとめる。**公開・再配布（Dockerイメージ公開・OSS公開等）を行う前に必ず確認すること。**
個人のローカル利用のみであれば実務上のリスクは低いが、他人に渡す・公開する時点で
各ライセンスの条件（表示義務・ソース開示義務等）が発生しうる。

ライセンス表記は `pip` パッケージのインストール済みメタデータ（`importlib.metadata`）と
各モデル配布元の記載を実際に確認した上で記録している（2026-07-10時点、Docker/RAW関連追加分は
2026-07-24に確認）。パッケージのバージョンアップやモデル差し替え時は再確認すること。

---

## PhotoFinder 自体のライセンス: AGPL-3.0（確定）

物体検出に使っている `models/yolo/yolov8x.onnx`（`tools/download_models.py` の
`export_yolo()` が Ultralytics 公式 GitHub Release の `yolov8x.pt` からその場で
ONNX へ変換して生成。当初の yolov8n は非公式 HuggingFace 再配布
`s1777/yolo-v8n-onnx` から取得していたが、2026-07-14 の YOLOv8m 移行時に
出所の明確な公式一次配布へ切り替え、2026-07-19 に GPU (CUDA) 対応と合わせて
精度優先で YOLOv8x へ再変更した）は
**Ultralytics 社の YOLOv8 の学習済み重みそのもの**であり、Ultralytics 公式サイト
（<https://www.ultralytics.com/license>）に明記されている通り

> すべての Ultralytics YOLO 学習済みモデルはデフォルトで AGPL-3.0 ライセンスの対象。
> 商用利用（非公開のまま配布したい場合を含む）には Enterprise License（有料）が必要。

この YOLOv8 系モデルを残す方針としたため、**PhotoFinder 自体も [LICENSE](../LICENSE) の通り
AGPL-3.0 で公開する**ことに決定した（配布物に AGPL-3.0 のコンポーネントを含む以上、
配布物全体をより緩いライセンスと称するのは実態と矛盾するため、一貫性のある選択として
AGPL-3.0 を採用）。README.md の「ライセンス」節も参照。

なお ONNX 変換に使う `ultralytics` パッケージ自体も AGPL-3.0 だが、これは
**モデル変換時（ビルド前の一度きり）のみ一時インストールして使う開発ツール**であり、
requirements.txt にもDockerイメージにも含まれない（変換手順は
`tools/download_models.py` の docstring 参照）。

AGPL-3.0 は「配布」に加え、**ネットワーク経由でソフトウェアと対話できる状態にした
時点で**ソース開示義務が生じる「ネットワーク条項」（第13条）を持つ。photofinderは
Dockerの`docker-compose.yml`で既定`127.0.0.1`バインドのみに制限しているが、
`ports:`の設定を変えてLAN/インターネットに公開した場合はこの条項の対象になる点に
留意すること（docs/docker.md参照）。

---

## Python 依存パッケージ（実行時, requirements.txt）

| パッケージ | ライセンス | 備考 |
|---|---|---|
| fastapi | MIT | |
| uvicorn[standard] | BSD-3-Clause | |
| python-multipart | Apache-2.0 | |
| pillow | MIT-CMU（通称 "Pillow License"、旧HPND） | |
| pillow-heif | BSD-3-Clause（Pythonバインディング自体） | ⚠️ PyPI分類子に GPLv2 の表記もあり要注意（下記参照） |
| piexif | MIT | |
| xxhash | BSD-2-Clause | |
| faiss-cpu | MIT | |
| onnxruntime | MIT | CPUイメージで使用。CUDAイメージでは `onnxruntime-gpu`（requirements-gpu.txt、同じくMIT）に差し替わる（Dockerfileのビルド引数`VARIANT`参照）。無印onnxruntimeと同居できないため両方を同時インストールすることはない |
| nvidia-cudnn-cu13（requirements-gpu.txt、CUDAイメージのみ） | NVIDIA Proprietary Software License Agreement | cuDNN本体はNVIDIA独自ライセンス（OSSではない）。再配布はpipホイール経由でNVIDIA自身が行っている。**`photofinder:cuda`イメージを第三者に配布/公開する場合、この依存も配布物に含まれる**点に注意 |
| numpy | BSD-3-Clause / 0BSD / MIT / Zlib / CC0-1.0（バンドル部品込み、いずれも許諾的） | |
| tokenizers | Apache-2.0 | |
| huggingface_hub | Apache-2.0 | |
| sudachipy | Apache-2.0 | |
| sudachidict-core | Apache-2.0 | 辞書データ本体。下記「データ」節も参照 |
| rapidocr-onnxruntime | Apache-2.0 | 同梱の検出/文字方向分類モデルも同ライセンス（PaddleOCR系、下記参照） |
| reverse_geocoder | LGPL | パッケージ本体のコードのライセンス。同梱データ（GeoNames）は別途 CC BY 4.0（下記「データ」節） |
| rawpy | MIT | 同梱の`libraw_r.so`（LibRaw、動的リンク）はLGPL-2.1。下記「rawpy / LibRaw について」参照 |
| exifread | BSD-3-Clause | RAWファイルのEXIF抽出フォールバック用（`photofinder/exif_utils.py`） |

### pillow-heif の GPLv2 表記について

`pip` のライセンス分類子に `GNU General Public License v2 (GPLv2)` が付与されているが、
プロジェクト本体（Python バインディング）は BSD-3-Clause と明記されている
（<https://github.com/bigcat88/pillow_heif>）。これは HEIC のデコード/エンコードを担う
ネイティブライブラリ（libheif: LGPL-3.0、HEVC エンコードを使う場合の libx265: GPL-2.0、
商用ライセンスとの二重ライセンス）がプラットフォームによって同梱されることに起因する。

**実機確認済み（Docker Linuxイメージで確認、2026-07-24）**:
manylinuxホイールにも `libheif-*.so`・`libde265-*.so` に加えて
**`libx265-*.so` が実際に同梱されている**ことを確認した（`pillow_heif.libs/`配下）。
PhotoFinder は HEIC の
**デコード（読み込み）のみ**を行い、エンコード（書き出し）は一切しないため、実行時に
x265 のコード（HEVCエンコーダ）が実際に呼び出されることは無いはずだが、
**バイナリとしてはDockerイメージに含まれている**という事実は変わらない。
x265 は GPL-2.0（`videolan/x265` の `COPYING` で確認）であり、GPL-2.0 単体は
AGPL-3.0（PhotoFinder全体のライセンス）と厳密には「結合」不可の組み合わせだが、
x265 は PhotoFinder のコードから直接呼ばれず libheif が内部的に（未使用のまま）
リンクしているだけの別バイナリであり、FSF の見解する「単なる集積 (mere aggregation)」に
留まる可能性が高い。とはいえグレーゾーンであることに変わりはなく、**対応済み**:
x265 の `COPYING`（GPL-2.0全文、`docs/third-party-licenses/x265-COPYING.txt`）を
`Dockerfile`がイメージ内 `/app/THIRD-PARTY-LICENSES/x265-COPYING.txt` に同梱するよう
にした（未使用のx265機能ごとビルドから除外する、という代替案は追加作業が必要なため
見送り）。

## rawpy / LibRaw について

RAW対応 (`photofinder/raw_utils.py`) で追加した `rawpy` (MIT) は `libraw_r.so`
（LibRaw の「redistributable」ビルド、GPL専用のデモザイクアルゴリズムを含まない
構成）を動的リンクで同梱する。LibRaw自体は **LGPL-2.1** で配布されており
（`rawpy`のwheelに同梱される`LICENSE.LibRaw`で確認済み。CDDL-1.0とのデュアル
ライセンスだが、ここではLGPL-2.1を選択した配布形態）、動的リンクである限り
LGPL-2.1はリンクする側（PhotoFinder）にAGPL/GPL化を要求しない。既にAGPL-3.0を
採用しているPhotoFinderにとって、LGPL-2.1コンポーネントの追加はライセンス選定に
影響しない（pillow-heif/libheifと同様の扱い）。

## Dockerイメージに同梱するフォント

透かし書き出し機能 (`photofinder/export.py` の `FONTS`) 用にaptパッケージとして
Dockerイメージへ同梱している。Pythonパッケージではないため上記の表には含めず、
ここに独立して記録する。

| パッケージ | 書体 | ライセンス | 備考 |
|---|---|---|---|
| `fonts-noto-cjk` | Noto Sans/Serif JP (CJK統合) | **OFL-1.1** | 元々HEIC等とは無関係にCJK全般の表示用に導入済み。透かしの既定フォント |
| `fonts-mplus` | M+ 1 (Regular/Bold) | **OFL-1.1** | 実機で `apt-cache search`/インストール後の `/usr/share/doc/fonts-mplus/copyright` を確認し特定。既存のNoto系と異なり本物の太字面を持つ。候補として `fonts-ipafont`/`fonts-ipaexfont`/`fonts-takao`（いずれもIPA Font License 1.0、改変時の名称変更義務あり）/`fonts-vlgothic`（M+Font/Sazanami/BSD-3-Clauseの混在）も検討したが、OFL-1.1が最も制約が緩く既存方針と相性が良いため採用 |

OFL-1.1 (SIL Open Font License) は同梱・再配布・改変を明示的に許可する設計のフォント
専用ライセンスで、AGPL-3.0のPhotoFinderに同梱してもライセンス選定に影響しない。

## 学習済みモデル

| モデル | 取得元 | ライセンス | 備考 |
|---|---|---|---|
| SigLIP2（多言語）画像/テキスト埋め込み | `onnx-community/siglip2-so400m-patch14-384-ONNX`（HF, ONNX変換版）。元モデルは `google/siglip2-so400m-patch14-384` | **Apache-2.0**（元モデルのHFページで確認済み） | 変換版自体のライセンス表記は無いが、元モデルの許諾条件を引き継ぐと解するのが妥当。2026-07-19: 精度優先で SigLIP (base, 768d/256px, `pulsejet/siglip-base-patch16-256-multilingual-onnx`) から SigLIP2 (so400m, 1152d/384px) へ変更。WebLI 109言語で学習済み(日本語含む) |
| YOLOv8x 物体検出 | Ultralytics公式 `yolov8x.pt`（GitHub Releases）から `tools/download_models.py` がその場でONNXへエクスポート（非公式HF再配布には依存しない） | **AGPL-3.0**（Ultralytics公式方針、上記参照） | PhotoFinder全体をAGPL-3.0とすることで対応済み。2026-07-14: 精度優先でnano→mediumへ変更、2026-07-19: GPU (CUDA) 対応と合わせて medium→x へ再変更（docs/design.md差分参照）。取得元は一貫して一次情報（公式.pt→ローカル変換） |
| 日本語OCR認識（japan_PP-OCRv3） | `cycloneboy/japan_PP-OCRv3_rec_infer`（HF） | Apache-2.0 の可能性が高い（PaddleOCR本体がApache-2.0のため） | 再配布元にライセンス表記なし。要一次情報での裏取り推奨 |
| OCR検出/文字方向分類モデル | `rapidocr-onnxruntime` パッケージ同梱（PaddleOCR系） | Apache-2.0（パッケージ自体がApache-2.0） | |
| 野鳥種名・色タグ推定 | 追加モデル無し（SigLIP埋め込みのゼロショット流用のみ） | 上記SigLIPに同じ | `photofinder/species_ja.py` の種リストはこのプロジェクト独自の著作物 |

## 外部データ

| データ | 提供元 | ライセンス | 帰属表示（必須） |
|---|---|---|---|
| 都市データ（オフライン逆ジオコーディング） | GeoNames（`reverse_geocoder` パッケージに同梱の `rg_cities1000.csv`） | **CC BY 4.0** | "This work uses data from GeoNames.org, licensed under CC BY 4.0." 等 |
| POI（建物/スポット名） | OpenStreetMap（Overpass API 経由で `data/poi.db` へ取得・キャッシュ） | **ODbL 1.0**（データベース自体）。個々の地物情報はコミュニティ提供 | "© OpenStreetMap contributors"（<https://www.openstreetmap.org/copyright> 参照） |
| 日本語形態素辞書 | SudachiDict（`sudachidict-core`） | Apache-2.0 | パッケージ表記の通りで別途の表示義務は無い（Apache-2.0は帰属表示が緩やか。NOTICEファイルがあれば転記が望ましい） |

### 実務上の対応

- GeoNames・OSM の帰属表示は、アプリ内（設定画面や README）に一箇所まとめて記載すれば足りる。
  例文:
  ```
  地名情報の一部に GeoNames.org のデータ（CC BY 4.0）および
  OpenStreetMap のデータ（© OpenStreetMap contributors, ODbL）を使用しています。
  ```
- OSM データは Overpass API から**都度取得してローカルにキャッシュ**する方式であり、
  データベース自体を同梱・再配布はしていない（`data/poi.db` は `.gitignore` 対象、
  Dockerイメージにも同梱しない設計）。ODbL の「データベースの共有」に関する厳格な義務
  （Share-Alikeでの再配布時の完全複製提供等）は、現状のアーキテクチャでは発生しにくいが、
  帰属表示自体は利用している事実がある限り必要。

---

## ライセンス選定の経緯（記録）

検討時点では以下の2択があった:

- **YOLOv8n を残す** → 配布物全体が事実上 AGPL-3.0 に拘束されるため、PhotoFinder 自体も
  AGPL-3.0 で公開するのが唯一の一貫した選択（Enterprise License を別途購入しない限り）
- **YOLOv8n を差し替える/落とす** → 他の依存はほぼ全て許諾的ライセンス
  （MIT/BSD/Apache-2.0）なので、Apache-2.0 や MIT 等の緩いライセンスも選べた

物体検出タグ機能を維持する判断から **YOLOv8n を残す方針**となり、PhotoFinder 自体も
**AGPL-3.0** で公開することに決定した。将来 YOLOv8n を非AGPLの検出モデルに差し替える
場合は、このライセンス選定自体を再検討してよい（その場合でも AGPL-3.0 → より緩い
ライセンスへの変更は権利者の同意なく行えるため障害はない。逆方向は不可）。

（追記 2026-07-14: 検出モデルを YOLOv8n → YOLOv8m へ変更したが、どちらも同じ
Ultralytics AGPL-3.0 条件のため、上記の経緯と結論はそのまま有効。）

（追記 2026-07-19: GPU (CUDA) 対応に合わせて検出モデルを YOLOv8m → YOLOv8x へ、
埋め込みモデルを SigLIP (base, Apache-2.0) → SigLIP2 (so400m, Apache-2.0) へ変更。
いずれもライセンス区分に変更なし（YOLOv8系は引き続きAGPL-3.0、SigLIP系は引き続き
Apache-2.0）のため、上記の経緯と結論はそのまま有効。）

（追記 2026-07-24: photofinderとしてDocker専用配布 (CPU/CUDA) に移行。RAW対応で
追加した `rawpy`(MIT, LibRaw LGPL-2.1を動的リンク同梱) と `exifread`(BSD-3-Clause)
はいずれも許諾的/LGPLで、既存のAGPL-3.0の結論に影響しない。`nvidia-cudnn-cu13`
(NVIDIA独自ライセンス) は `photofinder:cuda` イメージ自体の配布物に含まれる形に
変わった点に注意（上記表の該当行参照）。）
