-- PhotoFinder スキーマ v1 (docs/data-schema.md 準拠)
-- M1 では embedding/FTS/検出系テーブルも作成しておく（M2 以降で使用）

CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT);
INSERT OR IGNORE INTO schema_meta VALUES ('schema_version', '7');

-- アプリ設定 (key-value)。既定値はここで播種し、変更は PATCH /api/settings
CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO app_settings VALUES ('scan_on_startup', '1');

CREATE TABLE IF NOT EXISTS roots (
    id                INTEGER PRIMARY KEY,
    path              TEXT NOT NULL UNIQUE,
    ext_filter        TEXT NOT NULL DEFAULT 'jpg;jpeg;png;heic',
    scan_interval_sec INTEGER NOT NULL DEFAULT 900,
    enabled           INTEGER NOT NULL DEFAULT 1,
    recursive         INTEGER NOT NULL DEFAULT 1   -- 0 ならルート直下のみ走査
);

CREATE TABLE IF NOT EXISTS photos (
    id          INTEGER PRIMARY KEY,
    root_id     INTEGER NOT NULL REFERENCES roots(id),
    path        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL    NOT NULL,
    xxhash      TEXT    NOT NULL,
    phash       BLOB,
    ext         TEXT    NOT NULL,
    -- RAW (raw_utils.RAW_EXTS) は埋め込みプレビュー由来の解像度。
    -- センサーのネイティブ解像度ではない (raw_utils.py 参照)
    width       INTEGER, height INTEGER,
    taken_at    TEXT,
    index_state TEXT NOT NULL DEFAULT 'pending',
    ml_version  INTEGER NOT NULL DEFAULT 0,   -- 適用済み ML パイプラインの版。旧い写真はバックフィル対象
    deleted     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    exported_at TEXT,   -- 最後に書き出し(export)に成功したUTC時刻。NULLなら未書き出し
    UNIQUE (root_id, path)
);
CREATE INDEX IF NOT EXISTS idx_photos_hash  ON photos(xxhash);
CREATE INDEX IF NOT EXISTS idx_photos_taken ON photos(taken_at) WHERE deleted = 0;
CREATE INDEX IF NOT EXISTS idx_photos_state ON photos(index_state) WHERE index_state != 'complete';

CREATE TABLE IF NOT EXISTS exif (
    photo_id          INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
    camera_make       TEXT, camera_model TEXT, lens_model TEXT,
    focal_length_mm   REAL, f_number REAL, shutter_speed TEXT, iso INTEGER,
    gps_lat REAL, gps_lon REAL, gps_alt REAL, gps_img_direction REAL,
    raw_json          TEXT
);
CREATE INDEX IF NOT EXISTS idx_exif_camera ON exif(camera_model);

CREATE VIRTUAL TABLE IF NOT EXISTS photo_rtree USING rtree(
    photo_id, min_lat, max_lat, min_lon, max_lon
);

CREATE TABLE IF NOT EXISTS geo (
    photo_id   INTEGER PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
    country TEXT, prefecture TEXT, city TEXT,
    poi_name TEXT, poi_conf REAL,
    poi_alt  TEXT,   -- 周辺の主要POI名 (空白区切り)。検索用 (例: 地主神社の写真を「清水寺」で引く)
    poi_source TEXT CHECK (poi_source IN ('osm_nearby','landmark_clf','manual'))
);

CREATE TABLE IF NOT EXISTS detections (
    id       INTEGER PRIMARY KEY,
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    label    TEXT NOT NULL,
    conf     REAL NOT NULL,
    bbox     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_det_photo ON detections(photo_id);

CREATE TABLE IF NOT EXISTS bird_ids (
    detection_id INTEGER PRIMARY KEY REFERENCES detections(id) ON DELETE CASCADE,
    species_ja   TEXT NOT NULL,
    species_sci  TEXT,
    conf         REAL NOT NULL,
    topk_json    TEXT,
    confirmed    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS ocr_texts (
    id       INTEGER PRIMARY KEY,
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    text     TEXT NOT NULL,
    conf     REAL,
    bbox     TEXT
);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'manual'
         CHECK (kind IN ('auto','manual','species','place','ocr'))
);

CREATE TABLE IF NOT EXISTS photo_tags (
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags(id)   ON DELETE CASCADE,
    conf     REAL,
    source   TEXT NOT NULL DEFAULT 'user',
    verified INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (photo_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_ptags_tag ON photo_tags(tag_id);

-- 分かち書き済みテキストを格納 (rowid = photos.id)。contentless だと
-- 行の更新/削除ができないため通常の FTS5 テーブルにする (テキスト量は小さい)
CREATE VIRTUAL TABLE IF NOT EXISTS photos_fts USING fts5(
    tags_text, ocr_text, place_text, caption,
    tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS faiss_pending (
    photo_id INTEGER PRIMARY KEY,
    op       TEXT NOT NULL CHECK (op IN ('add','remove')),
    vector   BLOB
);

-- SNS (X/Instagram/その他) への投稿リンク。1枚の写真に複数投稿を許容 (再投稿・スレッド等)
CREATE TABLE IF NOT EXISTS photo_posts (
    id              INTEGER PRIMARY KEY,
    photo_id        INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    url             TEXT NOT NULL,
    posted_at       TEXT,    -- 判明していれば ISO8601 (現状は手動入力のみ、自動取得は未対応)
    caption_snippet TEXT,    -- oEmbed から取得したツイート本文の抜粋 (X のみ。取得失敗/対象外は NULL)
    source          TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual','archive')),
    -- platform: 投稿先SNS (x/instagram/other)。source (manual/archive、入力経路) とは独立した軸
    platform        TEXT NOT NULL DEFAULT 'x' CHECK (platform IN ('x','instagram','other')),
    platform_label  TEXT,    -- platform='other' の時だけ使う任意の表示名 (例: "Tumblr")
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_photo_posts_photo ON photo_posts(photo_id);
