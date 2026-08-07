# PhotoFinder データスキーマ

ストア構成: **SQLite（メタデータ + FTS5 全文）** / **FAISS（埋め込みベクトル）** / **ファイルシステム（サムネ WebP）**。
FAISS の vector ID = `photos.id` として結合し、対応表を持たない。

## ER 図

```mermaid
erDiagram
    ROOTS ||--o{ PHOTOS : contains
    PHOTOS ||--o| EXIF : has
    PHOTOS ||--o{ DETECTIONS : has
    PHOTOS ||--o{ OCR_TEXTS : has
    PHOTOS ||--o{ PHOTO_TAGS : has
    TAGS   ||--o{ PHOTO_TAGS : labels
    PHOTOS ||--o| GEO : has
    PHOTOS ||--o| PHOTOS_FTS : indexed_by
    DETECTIONS ||--o| BIRD_IDS : refined_by
    PHOTOS ||--o{ PHOTO_POSTS : linked_to

    ROOTS {
        int id PK
        text path "走査ルート (UNC可)"
        text ext_filter "jpg;jpeg;png;heic;..."
        int scan_interval_sec "未使用 (定期スキャンは不採用)"
        int enabled
        int recursive "0=直下のみ 1=サブフォルダ含む(既定)"
    }
    PHOTOS {
        int id PK "== FAISS vector id"
        int root_id FK
        text path "ルート相対パス"
        int size
        real mtime
        text xxhash "16hex 差分/移動検知"
        blob phash "8byte 原本特定用"
        text ext "RAWも含む (raw_utils.RAW_EXTS)"
        int width "RAWは埋め込みプレビュー解像度(センサー原寸ではない)"
        int height "同上"
        text taken_at "ISO8601, EXIF優先"
        text index_state "pending|error|complete"
        int ml_version "適用済みMLパイプライン版。バックフィル判定に使用"
        int deleted "tombstone"
        text created_at
        text updated_at
        text exported_at "最後に書き出し成功したUTC時刻。NULLなら未書き出し"
    }
    EXIF {
        int photo_id PK_FK
        text camera_make
        text camera_model
        text lens_model
        real focal_length_mm
        real f_number
        text shutter_speed
        int iso
        real gps_lat
        real gps_lon
        real gps_alt
        real gps_img_direction
        text raw_json "全EXIFのJSON退避"
    }
    GEO {
        int photo_id PK_FK
        text country
        text prefecture
        text city
        text poi_name "建物/POI名 (最寄り、data/poi.db 参照)"
        real poi_conf
        text poi_alt "周辺の主要POI名(空白区切り)。地主神社の写真を清水寺でも検索可能にする"
        text poi_source "osm_nearby|manual (landmark_clf は未実装)"
    }
    DETECTIONS {
        int id PK
        int photo_id FK
        text label "yolo クラス名"
        real conf
        text bbox "x,y,w,h (作業画像比率)"
    }
    BIRD_IDS {
        int detection_id PK_FK
        text species_ja "和名"
        text species_sci "学名"
        real conf
        text topk_json "上位3候補"
        int confirmed "常に0のまま (既存UIの確定/否認操作はPHOTO_TAGS.verifiedのみ更新し、
                       このカラムは触らない。種名タグの確定/否認状態を見るなら
                       PHOTO_TAGS.verified側を種名タグ名で突き合わせること。
                       dataset_export.pyのspecies_detailゲートも同様の理由でこちらを使う"
    }
    OCR_TEXTS {
        int id PK
        int photo_id FK
        text text
        real conf
        text bbox
    }
    TAGS {
        int id PK
        text name UK
        text kind "auto|manual|species|place|ocr"
    }
    PHOTO_TAGS {
        int photo_id PK_FK
        int tag_id PK_FK
        real conf "autoのみ"
        text source "yolo|bird|geo|user|..."
        int verified "0=未レビュー(既定) 1=ユーザ確定 -1=ユーザ否認。
                     dataset_export.pyはverified!=0の行のみを人手の正解データとして書き出す"
    }
    PHOTOS_FTS {
        text tags_text "分かち書き済み"
        text ocr_text
        text place_text
        text caption "photo_posts.caption_snippet の集約"
    }
    PHOTO_POSTS {
        int id PK
        int photo_id FK
        text url "投稿URL (X/Instagram/その他)"
        text posted_at "判明していればISO8601。現状は手動入力のみ"
        text caption_snippet "oEmbedから取得したツイート本文の抜粋 (Xのみ)"
        text source "manual|archive (archiveはXデータアーカイブ取込)"
        text platform "x|instagram|other。投稿先SNS種別 (sourceとは独立)"
        text platform_label "platform='other'の時のみ使う任意の表示名"
        text note
        text created_at
    }
```

## SQLite DDL

**唯一の定義元は [photofinder/schema.sql](../photofinder/schema.sql)**（本書に DDL を複製すると
ドリフトするため撤去）。マイグレーションは photofinder/db.py の _migrate が起動時に適用する。

スキーマ版の履歴:

| 版 | 変更 |
|---|---|
| v1 | 初期スキーマ（本書 ER 図の骨格） |
| v2 | photos.ml_version 追加（ML パイプライン版によるバックフィル判定）。photos_fts を contentless から通常 FTS5 へ（行の更新/削除を可能にするため） |
| v3 | geo.poi_alt 追加（周辺主要 POI 名。「地主神社」の写真を「清水寺」で検索可能にする）。app_settings テーブル追加（scan_on_startup, backup_auto） |
| v4 | roots.recursive 追加（フォルダごとに再帰/直下のみを切替） |
| v5 | photo_posts 追加（X投稿リンク・重複投稿警告機能。ALTER不要のCREATE TABLE IF NOT EXISTSのみのため _migrate() への追加コードなし） |
| v6 | photo_posts.platform/platform_label 追加（X以外のSNS投稿リンクにも対応）。photos.exported_at 追加（書き出し済みマーク表示用） |

ER 図との差分に気づいたら schema.sql を正としてください。

## POI データベース (data/poi.db) — メイン DB とは別ファイル

OSM から取得した POI（建物・スポット名）と、ユーザが手動追加したカスタム地点を格納する。
`photofinder/poi_fetch.py` が定義元。メイン DB (`photofinder.db`) からは参照されず、
`geo.py` の `poi_lookup()` が実行時に別接続で検索する（`geo.poi_name` へは値のコピーのみ保存）。

```sql
CREATE TABLE pois (
    id     INTEGER PRIMARY KEY,
    osm_id TEXT NOT NULL,     -- 'node/123' 'way/456' / 手動地点は 'manual/<uuid>'
    name   TEXT NOT NULL,
    kind   TEXT NOT NULL,     -- 'tourism=viewpoint' 等の代表タグ / 手動は 'manual'
    pref   TEXT NOT NULL,     -- 取得単位の都道府県名。手動地点は 'manual'
    lat REAL NOT NULL, lon REAL NOT NULL,
    UNIQUE (pref, osm_id)
);
CREATE VIRTUAL TABLE poi_rtree USING rtree(id, min_lat, max_lat, min_lon, max_lon);
CREATE TABLE poi_meta (pref TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, count INTEGER NOT NULL);
```

- **更新単位は都道府県**: `pref` ごとに丸ごと入れ替え（`store_pref`）。取得し直すとその県のデータだけ置き換わる
- **手動地点の pref は `'manual'`**: 近傍検索 (`poi_lookup`) は `pref` を問わず bbox 検索するため、
  カスタム地点も取得済み POI と同列に扱われ自動的にマッチ対象になる
- データ量の目安: 1都道府県あたり数千〜2万件程度（例: 京都府 17,358件、大阪府 19,837件、鳥取県 2,319件）

## FAISS 構成

| 項目 | 値 |
|---|---|
| ファイル | `data/vectors.faiss`（tmp 書き→rename でアトミック更新） |
| 型 | `IndexIDMap2(IndexHNSWFlat(ml.DIM, M=32))`、〜50万枚まで。`ml.DIM`は現在1152（SigLIP2 so400m、2026-07-19〜。旧768から変更）。次元変更時は`VectorStore`が不一致を検知して自動再構築（`photofinder/vectors.py`参照） |
| HNSWパラメータ | `efConstruction=200`（旧: FAISS既定値40のまま未調整）。`efSearch`はクエリのk（main.pyのフィルタ絞り込み強度に応じた200〜2000の動的拡張）に合わせ`max(128, min(4000, k*2))`で毎回動的設定（photofinderで追加） |
| 距離 | 内積（ベクトルは L2 正規化済み → コサイン等価） |
| 削除 | tombstone（`photos.deleted=1` で検索後除外）。HNSW は remove 不可のため再インデックスで同一 id が重複しうる（検索時 dedupe） |
| 再構築 | `POST /api/index/rebuild-vectors`（設定 UI・バックアップ時オプションからも可）。既存ベクトルを reconstruct して重複・削除済み id を除去。画像の再エンコード不要 |

## サムネイル

```
data/thumbs/{xxhash[:2]}/{xxhash}.webp   -- 320px 長辺, q=80
data/previews/{xxhash[:2]}/{xxhash}.webp -- 1600px 長辺, 初回要求時に遅延生成
```
