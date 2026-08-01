# PhotoFinder REST API 仕様

- Base URL: `http://127.0.0.1:8686/api`
- 認証: **未実装**。photofinder2はDocker配布で、公開範囲は`docker-compose.yml`の`ports:`
  マッピングで制御する (既定は127.0.0.1のみ)。LAN/インターネット公開時のアクセス制御は
  利用者側の責任 (リバースプロキシでの認証・VPN等)。詳細は docs/docker.md 参照
- エラー形式: FastAPI 標準の `HTTPException` をそのまま使用 → `{ "detail": "エラーメッセージ" }`。

## エンドポイント一覧

| Method | Path | 概要 |
|---|---|---|
| GET | `/search` | ハイブリッド検索（自然言語 + フィルタ）。`order=asc\|desc` で時系列並び順切替。`posted=true\|false` で投稿リンク（プラットフォーム問わず）の有無を絞り込み（省略時は全件） |
| POST | `/search/by-image` | 画像類似検索・原本特定（multipart） |
| GET | `/photos/{id}` | 写真詳細（EXIF・タグ・検出・OCR・GEO） |
| GET | `/photos/{id}/thumb` | サムネ WebP。クライアント側（`thumb_url`）が `?h={xxhash}` を付与する（内容アドレス化。サーバは `h` を検証・要求しない＝無くても200を返すが、同じ id でも中身が変わればハッシュも変わるURLにしないと `Cache-Control: immutable` で古い画像がキャッシュに残る） |
| GET | `/photos/{id}/preview` | 1600px プレビュー WebP。thumb と同様、クライアント側で `?h={xxhash}` を付与する（サーバ未検証） |
| GET | `/photos/{id}/file` | 原本ファイル |
| POST | `/photos/{id}/open-in-explorer` | OS のファイルマネージャで場所を開く (Dockerコンテナ実行時は501) |
| POST | `/photos/{id}/export` | 切り出し + 透かし + メタデータ除去で書き出し。成功時 `photos.exported_at` を更新（書き出しファイル自体は永続化しない。「いつ書き出したか」のメタ情報のみ記録し、`/search` の `exported` バッジ表示に使う） |
| PATCH | `/photos/{id}/place` | 写真1枚の場所名を手動で上書き/解除（`poi_name:null` で自動判定に戻す） |
| POST | `/photos/{id}/tags` | 手動タグ追加 |
| DELETE | `/photos/{id}/tags/{tagId}` | タグ削除（手動）/ 否認（自動） |
| PATCH | `/photos/{id}/tags/{tagId}` | 自動タグの確定(1)/否認(-1)/取り消し(0) |
| GET | `/tags?q=` | タグオートコンプリート |
| POST | `/photos/{id}/posts` | SNS投稿リンクを追加 `{url, platform?, platform_label?, note?, posted_at?}`。`platform` は `x`(既定)\|`instagram`\|`other`、`other` の時のみ `platform_label` を保存。X公式oEmbedでのキャプション取得は `platform=x` の時のみベストエフォートで実行（失敗しても登録は続行）。応答に `duplicate_warnings`（類似度の高い投稿済み写真、下記参照） |
| DELETE | `/photos/{id}/posts/{postId}` | SNS投稿リンクを削除 |
| GET | `/photos/{id}/posts/similar` | この写真と類似度が高く、既に投稿済みの写真一覧（プラットフォーム問わず。重複投稿の警告用。ML未対応/未ベクトル化なら空配列） |
| GET | `/roots` | 走査ルート一覧（`photo_count`・`recursive` 付き） |
| POST | `/roots` | ルート追加（`recursive` で再帰/直下のみ指定。追加分を即インデックス） |
| DELETE | `/roots/{id}` | ルートをインデックスから除外（ファイルには触れない。スキャン中は 409） |
| POST | `/index/scan` | 手動スキャン開始（`{root_id?}` 省略時全体。指定フォルダのみの再スキャンにも使用） |
| POST | `/index/cancel` | 実行中スキャンに停止を要求（未処理分は次回スキャンで自動再開） |
| GET | `/index/status` | 進捗スナップショット（`total`・`eta_seconds`・`rate_per_min`・`cancel_requested`・`cancelled` 等。UI は 2 秒ポーリング） |
| POST | `/index/rebuild-vectors` | ベクトル索引を再構築（重複・削除済みベクトルを除去） |
| GET | `/settings` / PATCH `/settings` | アプリ設定（`scan_on_startup`, `backup_auto`）。未設定キーは全て既定値 `true` として返る |
| POST | `/shutdown` | アプリを終了する（設定画面の「終了」ボタンから呼ばれる。応答を返してから `os._exit()`。Dockerでは`docker stop`と実質等価だが、UIからの明示終了手段として維持） |
| GET | `/geo/poi-status` | OSM POI データの取得状況（県別件数・取得日、`pref="manual"` はカスタム地点） |
| POST | `/geo/refresh-poi` | POI 更新後、GPS 持ち全写真へ場所名を再適用（画像再解析なし） |
| GET | `/geo/prefectures` | 都道府県名 47件のリスト（POI 取得フォームのドロップダウン用） |
| GET | `/geo/missing-prefectures` | 写真の撮影地にあるが POI 未取得の都道府県（`photo_count` 付き） |
| POST | `/geo/poi/fetch` | `{pref}` の POI を Overpass API から取得（バックグラウンド実行） |
| GET | `/geo/poi/fetch-status` | POI 取得の進捗（`running`・`pref`・`error`） |
| DELETE | `/geo/poi/prefecture/{pref}` | 指定都道府県の POI データを削除 |
| GET | `/geo/poi/search?q=` | POI 名でオートコンプリート検索 |
| GET | `/geo/poi/custom` | ユーザ手動追加したカスタム地点の一覧 |
| POST | `/geo/poi` | カスタム地点を手動追加 `{name, lat, lon}`（近傍検索に自動反映） |
| DELETE | `/geo/poi/{id}` | 個別 POI（カスタム/取得済み問わず）を削除 |
| GET | `/export/options` | 書き出しダイアログのフォント選択肢一覧（`export.FONTS` が定義元） |
| POST | `/export/dataset` | 人手タグ付け済み写真をJSONL+画像zipで書き出す（教師/評価データセット化。`{include_negatives?, include_bird_detail?}`、既定どちらもtrue） |
| GET | `/backup/status` | バックアップ状況（自動有効/無効・スナップショット一覧） |
| POST | `/backup/snapshot` | DB スナップショット即時作成（`{rebuild:true}` でベクトル索引も再構築） |
| POST | `/backup/restore` | `{path}` で指定したスナップショットに復元（下記参照） |
| POST | `/archive/import` | Xデータアーカイブ(zip)をアップロードし取り込み開始（バックグラウンド実行、multipart。`date_from`/`date_to` (YYYY-MM-DD、任意) で投稿日を絞り込み可） |
| GET | `/archive/import-status` | 取り込み進捗・レビュー待ち候補一覧（`phase`, `done`/`total`, `candidates[]`） |
| POST | `/archive/import/confirm` | レビューで確認した候補を `photo_posts` へ確定登録 `{accepted:[{photo_id,tweet_id}]}` |
| POST | `/archive/import/dismiss` | レビュー待ち候補を何も確定せず破棄 |
| GET | `/archive/suggested-date-from` | 次回取り込みの `date_from` の目安（リンク済み投稿の最新日 − 3日）を返す |

---

## GET /search

| Query | 型 | 説明 |
|---|---|---|
| `q` | string | 自然言語クエリ（省略時はフィルタのみで全件ブラウズ） |
| `date_from` / `date_to` | ISO8601 | 撮影日時範囲 |
| `bbox` | `minLon,minLat,maxLon,maxLat` | GPS 矩形 |
| `place` | string | 地名（都道府県/市区町村/POI 部分一致） |
| `camera` | string(csv) | カメラ機種 |
| `ext` | string(csv) | 拡張子 |
| `tags` | string(csv) | タグ AND 条件 |
| `mode` | `hybrid`(既定) / `text` / `vector` | 検索モード |
| `order` | `desc`(既定) / `asc` | 時系列の並び順。**`q` が空（ブラウズ時）のみ有効**。`q` 指定時（ハイブリッド/テキスト/ベクトル検索）は RRF ランキングを優先するため無視される |
| `limit` / `offset` | int / int | ページング（既定 100。`next_cursor` は次の offset 値） |

**200 応答**

```json
{
  "items": [
    {
      "id": 12345,
      "thumb_url": "/api/photos/12345/thumb?h=a1b2c3d4e5f6a7b8",
      "taken_at": "2025-11-03T07:12:44",
      "width": 6000, "height": 4000, "ext": "jpg",
      "filename": "DSC01234.jpg",
      "top_tags": ["カワセミ", "鳥", "青い鳥"],
      "posted": false,
      "exported": false
    }
  ],
  "total_estimate": 143,
  "next_cursor": "100",
  "suggested_filters": []
}
```

`suggested_filters` はクエリ解析によるフィルタ候補（将来対応。現状は常に空配列）。

## POST /search/by-image

`multipart/form-data`: `image`（必須）, `limit`（既定 30）

```json
{
  "exact": [
    { "id": 12345, "thumb_url": "...", "path": "/photos/2025/11/DSC01234.ARW",
      "hamming": 4, "score": 0.98 }
  ],
  "similar": [
    { "id": 12377, "thumb_url": "...", "score": 0.83 }
  ]
}
```

`exact` = pHash ハミング距離 ≤ 10（SNS 再圧縮画像から原本特定するケース）。`similar` = 意味的類似。

## GET /photos/{id}

```json
{
  "id": 12345,
  "path": "/photos/2025/11/DSC01234.ARW",
  "taken_at": "2025-11-03T07:12:44",
  "size": 24812544, "width": 6000, "height": 4000,
  "exif": {
    "camera_make": "SONY", "camera_model": "ILCE-7M4",
    "lens_model": "FE 200-600mm F5.6-6.3 G OSS",
    "focal_length_mm": 600, "f_number": 6.3, "shutter_speed": "1/1600", "iso": 1600,
    "gps": { "lat": 35.0116, "lon": 135.7681, "direction": 210.5 }
  },
  "geo": { "prefecture": "京都府", "city": "京都市", "poi_name": "鴨川デルタ",
           "poi_conf": 0.9, "poi_source": "osm_nearby" },
  "detections": [
    { "label": "bird", "conf": 0.94, "bbox": [0.42, 0.31, 0.18, 0.22],
      "bird": { "species_ja": "カワセミ", "species_sci": "Alcedo atthis",
                "conf": 0.91, "confirmed": false,
                "topk": [["カワセミ", 0.91], ["ヤマセミ", 0.04], ["ブッポウソウ", 0.02]] } }
  ],
  "ocr": [],
  "tags": [
    { "id": 7,  "name": "カワセミ", "kind": "species", "source": "bird", "conf": 0.91, "verified": 0 },
    { "id": 21, "name": "お気に入り", "kind": "manual", "source": "user", "verified": 1 }
  ],
  "posts": [
    { "id": 1, "url": "https://x.com/example/status/123", "posted_at": null,
      "caption_snippet": "鴨川で見かけたカワセミ", "source": "manual",
      "platform": "x", "platform_label": null, "note": null,
      "created_at": "2026-07-11 04:40:25" }
  ]
}
```

## SNS投稿リンク・重複投稿防止 (`/photos/{id}/posts*`)

過去にSNS（X/Instagram/その他）へ投稿した写真と投稿URLを手動で紐づける機能。1枚の写真に
複数の投稿リンクを許容する（再投稿・スレッド分割・複数プラットフォームへの投稿等に対応）。
投稿先は `platform` (`x`|`instagram`|`other`) で区別し、`other` の場合のみ任意の表示名を
`platform_label` に保存できる。X側の画像は切り抜き・透かしでファイルが完全一致しないことが
多いため、重複投稿の警告は xxhash/pHash の完全一致ではなく SigLIP 埋め込みのコサイン類似度で
行う（`GET /photos/{id}/posts/similar`、閾値 `SIMILAR_POST_MIN_SCORE=0.75` は `main.py` 内の
暫定値。実運用で誤検知/見逃しを見ながら調整する前提。プラットフォーム問わず全ての投稿済み写真
が対象）。X公式oEmbedによるキャプション取得（`caption_snippet`）は `platform=x` の投稿のみ
ベストエフォートで実行される。

運用フロー:
1. SNSからダウンロードした投稿済み画像を検索バーにドロップ/貼り付け（既存の `/search/by-image`
   をそのまま流用した逆引き検索）→ 類似候補から該当するローカル写真を選ぶ
2. 写真詳細パネルの「SNS投稿」欄でプラットフォーム（X/Instagram/その他）を選び、投稿URLを
   貼り付けて追加
3. 追加時、類似度の高い「既に投稿済みの写真」があれば `duplicate_warnings` で警告
   （ブロックはしない。最終判断はユーザー）
4. 検索の `posted=true|false` フィルタで「投稿済み/未投稿」を一覧比較できる（プラットフォーム
   問わず、いずれかの投稿リンクがあれば `posted=true` 扱い）

`caption_snippet` は X公式 oEmbed (`https://publish.twitter.com/oembed`) からのベストエフォート
取得（HTMLタグを除去した本文の先頭200文字）。ネットワーク不通や非公開ツイート等で取得できない
場合は `null` のまま登録され、機能はブロックされない。`posted_at` は現状 oEmbed から取得できない
ため常に手動入力（省略可）。`caption_snippet` は `photos_fts.caption` にも集約され、通常の
キーワード検索でツイート本文の一部からも該当写真を引けるようになっている。

## Xデータアーカイブ一括取り込み (`/archive/import*`)

過去のツイート全件を一括で照合・登録するフェーズ2機能。設定画面からXの
「データのアーカイブ」zip（`data/tweets.js` + `data/account.js` + `data/tweets_media/`）を
アップロードすると、`photofinder/archive_import.py` がバックグラウンドで:

1. `tweets.js` を解析してツイート一覧（ID・日時・本文・添付画像ファイル名）を抽出
2. `account.js` からアカウント名を取得できればツイートURLを `https://x.com/{handle}/status/{id}`
   の形で構築（取得できない場合も `https://x.com/i/status/{id}` という汎用URL形式で動作する）
3. 既に `photo_posts` にリンク済みのURLは対象から除外（再取り込みしても重複登録しない）
4. `date_from`/`date_to` (YYYY-MM-DD、任意・片側のみでも可) が指定されていれば、
   その期間に投稿されたツイート以外を除外（投稿数が多いと全件の埋め込みに時間が
   かかる ── 実測2,688枚で約16分 ── ため、期間を区切って複数回に分けて取り込む用途)
5. 添付画像を1枚ずつ SigLIP 埋め込みし、ローカル写真ライブラリと類似度照合
   （`ARCHIVE_MATCH_MIN_SCORE=0.75`、`/photos/{id}/posts/similar` と同じ暫定閾値）

**「前回の続きから」(`GET /archive/suggested-date-from`)**: Xのアーカイブ書き出しは
常に全期間のエクスポートで、サーバ側に差分取得の手段がない（申請から取得まで
1日程度かかることもあり、毎回全件を舐めるのは非効率）。そのためアプリ側で
「これまでにリンク済みの投稿のうち最新の投稿日」(`MAX(photo_posts.posted_at)`) を
覚えておき、そこから3日分のバッファを引いた日付を次回の `date_from` の目安として
返す。バッファはレビュー未確定のまま残っていた境界付近の古い候補を取りこぼさない
ための安全マージン。あくまで目安であり、確実に全件拾いたい場合は期間指定なしで
取り込むこと。

**自動では `photo_posts` へ書き込まない。** 閾値を超えた候補は `GET /archive/import-status` の
`candidates[]` としてのみ提示され、ユーザーが設定画面のレビューUIで確認・選択したものだけ
`POST /archive/import/confirm` で確定登録される（誤マッチのリンクを防ぐための必須ステップ）。
確定登録時の `source` は `'archive'`（手動リンクの `'manual'` と区別）。

**実アーカイブで検証済み (2026-07-12)**: ユーザー本人の実際のXデータアーカイブ（約1.7GB、
ツイート10,178件・写真2,688枚）で動作確認した。当初 `data/tweet_media/`（単数形）と
想定していたフォルダ名が実際には `data/tweets_media/`（複数形）だった点のみ実装と相違が
あり修正した。それ以外（`tweets.js` のプレフィックス・`tweet`ラッパー・`id_str`/`created_at`/
`full_text`/`extended_entities.media`のキー名、`account.js`の`account.username`、
`{tweet_id}-{media_basename}`というファイル名規則）はすべて想定通りだった。
実写真とアーカイブ内の実投稿画像（クロップ・透かしあり）とのSigLIP類似度照合も実施し、
意味的類似度による重複投稿検知が機能することを確認した。

## POST /photos/{id}/export

```json
{
  "crop": { "x": 0.1, "y": 0.05, "w": 0.6, "h": 0.6 },      // 省略可（0-1 正規化）
  "watermark": { "text": "© mikan", "position": "bottom-right",
                 "font": "gothic", "opacity": 0.6, "size_pct": 2.5 },   // 省略可（テキスト透かし）
  "strip_metadata": true,   // true = EXIF 全除去 (GPS・機材シリアル含む)。旧名 strip_gps も受理
  "format": "png", "quality": 95, "max_edge": null          // 既定: PNG・原寸。JPEG時のみ quality 使用
}
```

`watermark`はテキスト透かし（`text`必須）と画像透かし（`image_data_url`必須）の**どちらか一方**を指定する:

```json
{ "watermark": { "image_data_url": "data:image/png;base64,...", "image_size_pct": 20,
                  "position": "bottom-right", "opacity": 0.8 } }
```

`watermark.position` は 3x3 グリッドの8方向（中央除く）: `top-left`, `top`, `top-right`,
`left`, `right`, `bottom-left`, `bottom`, `bottom-right`。両方式で共通。
`watermark.font` は `GET /export/options` が返す id（既定 `gothic`＝Noto Sans JP）。テキスト方式のみ。
`watermark.size_pct`（テキスト、既定2.5・許容範囲0.5〜20）は書き出し画像の**高さ**に対するフォント
サイズの割合。`watermark.image_size_pct`（画像、既定20・許容範囲1〜100）は書き出し画像の**幅**に
対する透かし画像の幅の割合（アスペクト比は保持）。`image_data_url`はアルファチャンネル付き
PNG/WebP等を`FileReader`でdata URL化したもの（デコード後8MB・4000万px超は500エラーで拒否）。
画像自体のアルファに`opacity`をさらに掛け合わせて合成する。

→ 画像バイナリを `Content-Disposition: attachment` 付きで直接返す（`image/jpeg`|`image/png`|`image/webp`）。
コンテナ内には保存せず、ブラウザへそのままダウンロードさせる。**原本は変更しない。**

## POST /export/dataset

人手タグ付け済み写真を教師/評価データセット用にJSONL+画像zipとして書き出す
（`photofinder/dataset_export.py`）。対象は `photo_tags.verified != 0`
（人手が✓確定/✗否認した行のみ、未レビューのタグは対象外）。

```json
{ "include_negatives": true, "include_bird_detail": true }
```

→ `dataset-{timestamp}.zip` を直接返す（`Content-Disposition: attachment`）。中身:

```
dataset.jsonl        # 1行1写真
images/{xxhash}.webp # 1600pxプレビュー (オリジナルではない。GPS/EXIFを含まない)
```

`dataset.jsonl` の1行:

```json
{
  "photo_id": 12345, "xxhash": "a1b2c3d4e5f6a7b8",
  "image_path": "images/a1b2c3d4e5f6a7b8.webp",
  "width": 1600, "height": 1067, "taken_at": "2025-11-03T07:12:44",
  "camera_model": "ILCE-7M4",
  "positive_tags": [{"name": "カワセミ", "kind": "species", "source": "bird", "conf": 0.91}],
  "negative_tags": [{"name": "ヤマセミ", "kind": "species", "source": "bird", "conf": 0.4}],
  "species_detail": [
    {"bbox": "0.42,0.31,0.18,0.22", "yolo_conf": 0.94, "species_ja": "カワセミ",
     "species_sci": "Alcedo atthis", "species_conf": 0.91,
     "topk": [["カワセミ", 0.91], ["ヤマセミ", 0.04]], "verified": 1}
  ]
}
```

対象写真が0件（=1件も✓確定/✗否認したタグが無い）なら404。`negative_tags`は人手が
明示的に却下したAI提案（`verified=-1`）— `MIN_SIM`/`MIN_MARGIN`等の閾値再較正に有用な
ため既定で含める。`species_detail`は`detections`+`bird_ids`を`photo_tags.verified`
（種名タグ名で突き合わせ、`bird_ids.confirmed`は既存UIから更新されない別カラムのため
使わない）でゲートしたもの。

## POST /backup/restore

```json
{ "path": "/app/data/backup/photofinder-20260724-143202-255.db" }
```

`path`は`GET /backup/status`の`snapshots[].path`のいずれか（`data/backup/`配下のみ許可、
外は403）。誤って選んでも1つ前に戻せるよう、**復元前に現在の状態の安全スナップショットを
自動作成**してから復元する。復元は`photofinder.db`をスナップショットファイルで置き換える
処理で、生きたWALモード接続を持ったまま安全に差し替えるため**復元後にプロセスを終了**し、
Dockerの`restart: unless-stopped`ポリシーによる再起動を待つ（`/api/shutdown`と同じ
「差し替え→即終了→再起動時に開き直す」パターン）。

```json
{ "ok": true, "safety_snapshot": "/app/data/backup/photofinder-20260724-143202-309.db",
  "restored_from": "/app/data/backup/photofinder-20260724-143202-255.db", "restarting": true }
```

エラー: スキャン実行中は409、`path`未指定は422、`backup/`配下以外のパスは403、
存在しないファイルは404。復元直後はFAISS索引が古いDBの内容とズレうるが、検索結果の
`_hydrate()`は存在しないphoto_idを黙ってフィルタするだけでエラーにはならないため
自動再構築はしない（気になる場合は`POST /index/rebuild-vectors`を別途呼ぶ）。

## GET /index/status

```json
{
  "running": true,
  "phase": "extract",                   // scan | extract | flush | idle
  "queue": { "pending": 1240 },
  "total": 1550,                        // 今回スキャン対象の総数 (進捗バーの分母)
  "done_total": 310,
  "eta_seconds": 774.2,                 // 実測レートからの残り推定秒数 (extract フェーズのみ)
  "rate_per_min": 24.8,
  "cancel_requested": false,
  "cancelled": false,                   // 直近スキャンが POST /index/cancel で中断されたか
  "counts": { "added": 45, "updated": 3, "moved": 12, "deleted": 1, "skipped": 98760 },
  "current_root": "/photos/NAS-mirror",
  "errors_recent": [],
  "indexed_total": 99070, "vectors": 99070, "ml_available": true,
  "providers": {
    "siglip_text": null,                // 未ロード (初回使用まで遅延ロード)
    "siglip_vision": "CPUExecutionProvider",
    "yolo": "CPUExecutionProvider"      // CPUイメージでの既定値。CUDAイメージ+
                                         // GPU利用可なら "CUDAExecutionProvider" になる
  },
  "in_docker": true                     // フロントのreveal系UI出し分けに使用
}
```

進捗の受け取りは本エンドポイントのポーリング（UI は 2 秒間隔）。
SSE (`/index/events`) は当初設計にあったが未実装（必要になれば追加）。
停止（`POST /index/cancel`）時、未処理の写真は `index_state='pending'` のまま残るため、
次回スキャン（手動「今すぐスキャン」）で自動的に続きから処理される。

`providers` は各ONNXモデルが実際に使っている実行プロバイダ（`photofinder/ml.py`の
`ORT_PROVIDERS = ["CUDAExecutionProvider", "CPUExecutionProvider"]`のうち、実機で
有効だった方）。各モデルは初回使用（検索・スキャン）まで遅延ロードのため、未使用のうちは
`null`。CUDAイメージでもGPUが使えない環境（`nvidia-container-toolkit`未設定等）では
自動的に `CPUExecutionProvider` にフォールバックし、エラーにはならない（設定画面「AI処理」に表示）。
