# PhotoFinder 詳細設計書

対象読者: 本アプリを実装する開発者（＝あなた自身）。README の概要を前提とする。

> **本書は実装前に書いた当初の設計案であり、その後の開発で判断が変わった箇所がある。**
> 現在の実装状況は README の「実装状況」節が正（実際に動作確認済みの内容のみを記載）。
> 以下は当初案から変わった主な点。理由付きで残しているのは、後から読んで
> 「なぜそうしなかったか」が分かるようにするため。
>
> **本書はapp2/photofinder (フォーク元) の設計書をそのまま引き継いでいる。**
> 下表はapp2/photofinder時点での「当初案 vs 実装」の記録であり、photofinder2で
> さらに変わった点 (Docker配布・RAW対応・FAISSチューニング・データセット書き出し)
> は末尾の「photofinder2での追加変更」節を参照すること。

| 項目 | 本書の当初案 | 実装 | 理由 |
|---|---|---|---|
| インデクサの並列化 (§1) | `ProcessPoolExecutor`（CPU数-1） | 単一バックグラウンドスレッドで逐次処理 | 個人規模（〜数百万枚でなく数万〜数十万枚）では十分な速度が出た。プロセス間のDB接続管理・エラー伝搬の複雑さを避けた |
| リアルタイム監視 (§1, §5, §8) | Watcher コンポーネント（FSイベント）+ SSE で進捗プッシュ | 起動時スキャン（設定でON/OFF可）と手動ボタンのみ。進捗は `GET /index/status` の2秒ポーリング | ユーザ指定の仕様変更。差分スキャンが軽く手動運用で十分、watchdog固有のエッジケース対応コストが見合わないと判断（§8 に詳細） |
| OCR エンジン (§1, §2) | PaddleOCR (ONNX変換) | RapidOCR + japan_PP-OCRv3 認識モデル | 変換済みONNXの入手性・セットアップの簡便さで選定。実写看板で日本語/英語混在文の高精度読み取りを確認済み |
| 鳥種名推定 (§1, §7) | EfficientNetV2-S を CUB/NABirds+日本産鳥類で転移学習 | SigLIP ゼロショット（画像/種名テキストembeddingの類似度）。学習なし | 既製の Kaggle 525種分類器を試したところ日本の普通種（カワセミ等）がクラスに無く近縁種に誤答したため不採用。学習データ収集・学習コストも回避。理由は `photofinder/bird.py` docstring 参照 |
| 建物名推定・第二手段 (§7) | ランドマーク分類器（Google Landmarks v2） | 未実装。代わりにユーザによる POI 手動管理（都道府県取得/カスタム地点追加/写真ごとの場所名編集）を実装 | 分類器は誤爆対策が難しく個人宅周辺等では実用性が低い。ユーザが直接データを補える方が確実 |
| RAW 対応 (§2 step1) | 埋め込みプレビュー優先でデコード | app2/photofinderでは未対応だった（jpg/jpeg/png/heicのみ）。**photofinder2で当初案通りrawpy埋め込みプレビュー抽出により対応**（`photofinder/raw_utils.py`、詳細は末尾節） | app2/photofinder時点ではJPG運用にスコープを限定していたが、photofinder2で解消 |
| フロントエンド (§6) | React + Vite + TanStack Query/Virtual + Zustand + MapLibre | ビルド不要の単一 HTML + バニラ JS（`photofinder/static/index.html`） | 個人用途でビルドパイプラインの運用コストを避けた。React 版は `samples/typescript/` に参照実装として残すが未接続 |
| 地図表示 (§6 詳細パネル) | MapLibre + ローカルタイル/OSM | 未実装。場所は都道府県/市区町村/POI名のテキスト表示のみ | スコープ外。地図ウィジェットの追加は将来対応 |
| index_state (§2 要点) | `pending / meta_done / ml_done / complete` の4段階 | `pending / error / complete` の3段階（`ml_version` 列でパイプライン版によるバックフィルを別管理） | 実装を進める中で2段階の中間状態は不要と判明。エラー状態を明示する方が運用上有用だった |
| 性能目標 (§8) | 100枚/分/コア、10万枚規模で検索<200ms | 実写349枚での実測: 約20〜25枚/分（CPU、フルML込み）。検索応答は16〜160ms（実測） | 10万枚規模でのベンチマークは未実施。桁が大きく変わる可能性があるため参考値として扱うこと |
| 物体検出モデル (§1, §2) | YOLOv8n (6MB) | YOLOv8n→YOLOv8m（2026-07-14）→YOLOv8x（2026-07-19）。ONNX実行は `ORT_PROVIDERS = ["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"]`（CUDA→DirectML→CPUの順に優先、無ければ自動フォールバック） | ユーザ要望で精度優先。nano/medium/large/x等は出力形状 `(84, 8400)` が共通のため detector.py の decode/NMS は無改修。CPU単体では重いためDirectML (GPU/APU) 対応も同時実装。当初案(§1 ML Runtime)はCPU固定を想定していなかったが明記もしていなかったため厳密な「変更」ではない |
| 埋め込みモデル (§1, §2) | SigLIP base (768d) | SigLIP2 so400m（1152d/384px、2026-07-19〜）。WebLI 109言語（日本語含む）で学習済み | GPU (CUDA) 対応で推論余地が増えたため精度優先で変更。次元変更でFAISSインデックス・ゼロショット候補バンク(bird.py/colors.py)のキャッシュが非互換になるため、次元不一致検知による自動再構築とキャッシュキーへのDIM混入で対応（CLAUDE.md参照） |
| GPU実行プロバイダ (§1, §2) | 未記載（CPU前提） | Linux/WSL2 + NVIDIA GPU では `CUDAExecutionProvider` が実機(RTX 3060)で動作確認済み(2026-07-19)。Windows exeは引き続きCPU固定（DirectMLがRTX 3060実機でクラッシュしたため） | 開発機がWSL2+RTX 3060のため、Windows専用のDirectMLとは別に、より枯れたCUDA経路を追加。cuDNNはpipホイール(`nvidia-cudnn-cu13`)をctypesで明示プリロードする方式(`LD_LIBRARY_PATH`書き換えは起動後には効かないため不採用)。詳細はREADME「ローカル開発」節・CLAUDE.md参照 |

---

## 1. 全体構成とプロセスモデル

**単一 Python プロセス**に以下を同居させる。個人用途（〜数十万枚）ではプロセス分離のオーバーヘッドと運用コストが利益を上回るため。

| コンポーネント | 役割 | 実行コンテキスト |
|---|---|---|
| API Server | REST + SSE。検索・タグ編集・インデックス制御 | uvicorn イベントループ |
| Indexer | 走査→差分判定→抽出パイプライン | `ProcessPoolExecutor`（CPU数-1） |
| ML Runtime | ONNX セッション保持（SigLIP/YOLO/OCR/分類器） | Indexer ワーカ内 + API プロセス（クエリエンコード用に SigLIP text encoder のみ） |
| Thumbnail Service | WebP サムネ生成・配信 | インデックス時生成、配信は静的ファイル |
| Watcher | FS イベント / 定期スキャンのトリガ | バックグラウンドスレッド |

**メモリ予算目安**（軽快さの根拠）:
- SigLIP base ONNX: ~400MB → int8 量子化で ~110MB
- YOLOv8n: 6MB / PaddleOCR mobile: ~15MB / 鳥分類器: ~80MB
- FAISS HNSW (768d float16, 10万枚): ~160MB
- 合計常駐 ~500MB 以内。インデックス停止中は ML セッションを解放するオプションあり。

## 2. インデックスパイプライン

```
walk(roots, ext_filter)
  └─ for each file:
       diff_check(path, size, mtime)      … SQLite 照合。一致→skip
         ├─ 変更あり → xxhash64(先頭1MB+末尾1MB+size)   … フル読み込み回避
         │     ├─ hash 既知 → 移動/リネーム: photos.path 更新のみ (ML再処理なし)
         │     └─ hash 新規 → enqueue(extract_job)
extract_job(file):                         … ProcessPool 内、以下を1パスで
  1. decode        : HEIC/RAW は埋め込みプレビュー優先。長辺1024pxに縮小した作業画像を1枚作り全 ML で共有
  2. exif          : piexif/exifread → 日時, GPS, Make/Model/Lens, ISO/F/SS/焦点距離
  3. reverse_geo   : GPS → ローカル逆ジオ DB (国土数値情報 or OSM 抽出, SQLite R*Tree) → 都道府県/市区町村/POI
  4. thumbnail     : 320px WebP q=80 → data/thumbs/{hash[:2]}/{hash}.webp
  5. embedding     : SigLIP image encoder → 768d, L2正規化
  6. detect        : YOLOv8n → [{label, conf, bbox}]  (conf ≥ 0.35)
  7. bird_classify : detect 結果に bird があれば bbox 切り出し → 鳥分類器 → 種名 topk3
  8. ocr           : OCR heuristic(エッジ密度でテキスト存在推定) が真のときのみ PaddleOCR
  9. landmark      : GPS あり → POI 近傍 (≤150m) を建物名候補に。GPS 無し && 設定有効 → ランドマーク分類器
 10. commit        : SQLite トランザクション一括 INSERT + FTS5 + faiss_pending テーブルへ
faiss_flush (500件毎 or 60秒毎):
  faiss_pending → FAISS add_with_ids → index.write() アトミック置換 (tmp→rename)
```

**設計上の要点**
- 手順 1 の「作業画像1枚を全 ML タスクで共有」がスループットの鍵。デコードが最重コストのため。
- FAISS の ID には `photos.id`（SQLite rowid）をそのまま使い、対応表を持たない。
- 削除は FAISS `remove_ids`（HNSW は remove 不可のため tombstone 方式: `photos.deleted=1` で検索時除外、削除率 >10% で再構築）。
- 途中クラッシュ耐性: `photos.index_state`（pending/meta_done/ml_done/complete）で再開ポイント管理。

## 3. 検索設計（ハイブリッド検索）

### 3.1 自然言語検索フロー

```
query = "去年の秋に京都で撮ったカワセミ"
  │
  ├─ (a) クエリ解析(ルールベース+軽量辞書):
  │      日付表現 → date_from/date_to、地名辞書 → geo フィルタ候補
  │      ※ フィルタ「候補」として UI にチップ表示し、ユーザが外せる
  ├─ (b) FTS5 検索: タグ/種名/OCR/地名 に BM25 → text_score
  ├─ (c) SigLIP text encoder → 768d → FAISS top-200 → vec_score (cosine)
  └─ (d) 融合: RRF (Reciprocal Rank Fusion, k=60)
         final = 1/(60+rank_fts) + 1/(60+rank_vec)
         → SQL フィルタ(日時/場所/カメラ/タグ)を適用して上位 N 返却
```

RRF を選ぶ理由: スコア正規化不要で BM25 とコサイン類似度を安全に混ぜられる。実装3行。

### 3.2 画像類似検索（逆検索ワークフロー）

1. ユーザが画像をアップロード（SNS 保存画像・スクショ等、縮小/再圧縮/トリミング済み想定）
2. SigLIP image encoder → FAISS top-50
3. 上位候補に対し **pHash（64bit perceptual hash）** で再ランク。SigLIP は意味類似、pHash は同一画像判定に強い — 併用で「原本特定」精度を上げる
4. UI は「一致度: 同一の可能性 / 類似」の2段階表示

このため `photos.phash` をインデックス時に保存しておく（BK-tree は不要、top-50 に対する線形比較で十分）。

### 3.3 フィルタ

すべて SQLite WHERE 句に落ちる:
`taken_at BETWEEN`, `lat/lon 矩形 (R*Tree)`, `camera_model IN`, `ext IN`, `EXISTS (photo_tags)`

FAISS 側は先に top-K を広め（フィルタ強度に応じ 200〜2000）に取ってから SQL で絞る **post-filtering**。個人規模ではこれで十分で、実装が単純。

## 4. データ層

docs/data-schema.md を参照。要点のみ:
- SQLite は WAL モード、`synchronous=NORMAL`。
- FTS5 は external content テーブル（`photos_fts`）で二重保持を回避。
- 日本語トークン化: FTS5 標準では不可のため、**インデックス時に SudachiPy(または fugashi) で分かち書きした文字列を FTS 列に格納**する方式。トークナイザ拡張 DLL のビルドを避ける（Windows で軽快に動かすため）。

## 5. API 層

docs/api-spec.md 参照。設計原則:
- 検索は GET（URL 共有・ブラウザ履歴が効く）。画像アップロードのみ POST multipart。
- インデックス進捗は SSE (`/api/index/events`) でプッシュ。WebSocket は不要（単方向のため）。
- サムネ・原寸プレビューは `ETag: {xxhash}` + `Cache-Control: immutable` で二度目以降ゼロコスト。

## 6. フロントエンド設計

| 画面 | 構成要素 |
|---|---|
| 検索/一覧 | 検索バー（自然言語 + 画像ドロップ）、フィルタチップ、仮想スクロールグリッド（TanStack Virtual, justified layout） |
| 詳細パネル | 右スライドイン。EXIF、地図（GPS時, MapLibre+ローカルタイルorOSM）、自動タグ（信頼度バッジ付き・クリックで確定/否認）、手動タグ入力（オートコンプリート） |
| プレビュー | ライトボックス。切り出し（アスペクト比プリセット）＋透かし（テキスト/ロゴ、位置・透過度）→ エクスポート。**原本は不変更、書き出しのみ** |
| 逆検索 | 画像ドロップ → 同一候補/類似候補の2セクション表示 → 原本の場所をエクスプローラで開くボタン |
| 設定 | 対象フォルダ/拡張子、スキャン間隔、ML オン/オフ（OCR・鳥・建物）、プライバシー（クラウド機能トグル）、バックアップ |

状態管理: サーバ状態は TanStack Query、UI 状態は Zustand。検索条件は URL クエリに正規化（共有・戻る対応）。

## 7. 野鳥種名推定・建物名推定

### 野鳥
- ベース: EfficientNetV2-S（ImageNet21k 事前学習）
- データ: CUB-200 / NABirds で汎化 → 日本産鳥類（約 600 種、iNaturalist research-grade + 自前写真）でファインチューニング
- 学習レシピ: 入力 384px、RandAugment、label smoothing 0.1、クラス不均衡は logit adjustment
- 推論: YOLO の bird bbox を 1.2 倍パディングで切り出し → 分類。conf < 0.4 は「鳥（種名不明）」タグに留める
- **段階導入**: v1 は YOLO の "bird" タグのみ → v2 で分類器追加、が現実的

### 建物名
- 第一手段（軽量・高精度）: GPS → OSM POI（`building`, `historic`, `tourism` タグ）近傍 150m + 撮影方位（EXIF GPSImgDirection があれば方向 ±45° で絞る）
- 第二手段（オプション）: ランドマーク分類器（Google Landmarks v2 学習済みモデル）。誤爆しやすいので信頼度 0.7 未満は保存しない

## 8. 運用設計

### 差分・スキャンのタイミング【2026-07 仕様変更】
- **起動時スキャン**（設定 `scan_on_startup` で無効化可、既定 ON）と
  **手動「今すぐスキャン」ボタン**の2つのみ
- watchdog リアルタイム監視・定期スキャンは不採用に変更。
  理由: 差分判定が軽い（10万枚 ≈ 1〜2分）ため手動運用で十分であり、
  イベント駆動特有のエッジケース（コピー途中の不完全ファイル・イベント重複等）の
  実装/検証コストが見合わない。将来必要になれば diff_scan を呼ぶトリガー層として後付け可能

### プライバシー
- 既定: 全処理ローカル・`127.0.0.1` バインド・テレメトリなし
- クラウド OCR / オンライン地図タイル / オンライン逆ジオは個別トグル（既定 OFF）
- エクスポート時の GPS 除去オプション（SNS 投稿向け）

### バックアップ
- `data/` ディレクトリ = 全状態。ユーザ操作: フォルダコピーのみ
- 自動スナップショット: 週1 `VACUUM INTO data/backup/photofinder-YYYYMMDD.db`（世代3）
- FAISS・サムネは DB から再生成可能（免責事項として README 記載）。DB のみ死守
- スキーマに `schema_version` を持ち、起動時マイグレーション

### 性能目標

| 項目 | 目標 |
|---|---|
| 初回インデックス | 100枚/分/コア（フル ML 込み、CPU） |
| 差分スキャン | 10万枚のメタ照合 < 2分 |
| テキスト検索応答 | < 200ms (p95, 10万枚) |
| 画像類似検索応答 | < 400ms（アップロードデコード込み） |
| サムネグリッド | 60fps スクロール（仮想化 + WebP 320px） |

## 9. 主要コンポーネント擬似コード

### 9.1 差分判定

```
def diff_scan(root, exts):
    known = load_index_snapshot(root)          # {path: (size, mtime, id)}
    seen = set()
    for path in walk(root, exts):
        seen.add(path)
        rec = known.get(path)
        if rec and rec.size == stat.size and rec.mtime == stat.mtime:
            continue                            # 変更なし
        h = fast_hash(path)                     # 先頭1MB+末尾1MB+size の xxhash64
        if (dup := find_by_hash(h)) and not exists(dup.path):
            update_path(dup.id, path)           # 移動/リネーム
        else:
            enqueue_extract(path, h)            # 新規 or 内容変更
    for path in known.keys() - seen:
        mark_deleted(known[path].id)            # tombstone
```

### 9.2 ハイブリッド検索

```
def search(q, filters, limit):
    auto = parse_query_filters(q)               # 日付/地名 → フィルタ候補
    fts_ranks  = fts5_search(tokenize_ja(q), limit=200)
    qvec       = siglip_text_encode(q)
    vec_ranks  = faiss.search(qvec, k=widen(limit, filters))
    fused = rrf_fuse(fts_ranks, vec_ranks, k=60)
    ids = sql_filter(fused.ids, merge(filters, auto.accepted))
    return hydrate(ids[:limit]), auto.suggestions
```

### 9.3 逆検索（原本特定）

```
def reverse_search(uploaded_image):
    img   = decode_and_orient(uploaded_image)
    vec   = siglip_image_encode(img)
    cands = faiss.search(vec, k=50)
    ph    = phash64(img)
    for c in cands:
        c.ham = hamming(ph, db.phash[c.id])
    exact   = [c for c in cands if c.ham <= 10]    # ほぼ同一（再圧縮耐性）
    similar = [c for c in cands if c.ham > 10][:20]
    return exact, similar
```

## 10. 拡張ロードマップ（実装順の推奨）

1. **M1**: 走査 + EXIF + サムネ + SQLite + グリッド UI（検索はファイル名/日付のみ）
2. **M2**: SigLIP 埋め込み + FAISS + 自然言語/画像類似検索
3. **M3**: YOLO タグ + FTS5 ハイブリッド + タグ編集 UI
4. **M4**: OCR + 逆ジオ + 逆検索ワークフロー + 透かし/切り出し
5. **M5**: 鳥分類器 + 建物名 + watchdog リアルタイム + バックアップ自動化


---

## 11. photofinder2での追加変更

app2/photofinderからのフォーク後、以下を追加実装した(詳細はCLAUDE.md/docs/docker.md参照):

- **配布をDocker専用に変更**: Windows exe (PyInstaller) 配布を廃止。`Dockerfile`を
  ビルド引数`VARIANT=cpu|cuda`でCPU/CUDAの2バリアントに作り分ける。DirectML対応コード
  (Windows専用) は削除、`ORT_PROVIDERS`はCUDA→CPUのみに簡素化。
- **RAW対応**: `photofinder/raw_utils.py`が`rawpy`の`extract_thumb()`で埋め込み
  プレビューを抽出し、既存の共有ワーキングイメージパイプライン(§2 step1が当初から
  想定していた設計)にそのまま流し込む。フル現像はしない。EXIFは`piexif`が空を返す
  RAWファイルに限り`exifread`にフォールバックする。
- **FAISS/RRFチューニング**: `IndexHNSWFlat`に`efConstruction=200`を設定(旧: FAISS
  既定値40のまま未調整)、`efSearch`をクエリのkに応じて動的設定。§9.2の擬似コードが
  当初から意図していた`k=widen(limit, filters)`(フィルタの絞り込み強度に応じた
  top-k動的拡張、200〜2000)を実装し、`main.py`のk=200固定という実装との乖離を解消した。
- **データセット書き出し**: 既存の確定/否認タグ機構(`photo_tags.verified`)を人手の
  正解データとして扱い、`POST /api/export/dataset`で教師/評価データセット用の
  JSONL+画像zipを書き出す新機能(`photofinder/dataset_export.py`)。
