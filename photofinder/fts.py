"""FTS5 日本語全文検索 (M3)。

FTS5 標準トークナイザは日本語を分かち書きできないため、
SudachiPy (Mode C) で分かち書きした文字列を photos_fts に格納する
(docs/design.md §4)。クエリ側も同じ分かち書きを通して MATCH する。
"""
from __future__ import annotations

import sqlite3
import threading
import unicodedata

_lock = threading.Lock()
_tokenizer = None


def tokenize_ja(text: str) -> str:
    """分かち書き。'鴨川のカワセミ' → '鴨川 の カワセミ'。
    SudachiPy 未インストールなら分かち書きせず NFKC 正規化のみ適用して返す。

    NFKC 正規化を先に通す: OCR は看板等から全角英数 (「ＯＰＥＮ」「２０２５」) や
    半角カナを拾うことが多く、FTS5 の unicode61 トークナイザは全角/半角を同一視
    しないため、索引・クエリ両側をここで半角英数・全角カナに揃える。
    索引側の反映は scanner.ML_VERSION のバックフィルに乗る (v9 で全写真再索引)。
    """
    global _tokenizer
    text = unicodedata.normalize("NFKC", text)
    if not text.strip():
        return ""
    with _lock:
        if _tokenizer is None:
            try:
                from sudachipy import dictionary, tokenizer as sudachi_tok
                _tokenizer = (dictionary.Dictionary().create(),
                              sudachi_tok.Tokenizer.SplitMode.C)
            except ImportError:
                _tokenizer = (None, None)
    tok, mode = _tokenizer
    if tok is None:
        return text
    return " ".join(m.surface() for m in tok.tokenize(text, mode))


def update_fts(db: sqlite3.Connection, photo_id: int) -> None:
    """DB の現状態から photos_fts の1行を再構築する。タグ変更・抽出完了時に呼ぶ。"""
    tags = [r["name"] for r in db.execute(
        """SELECT t.name FROM photo_tags pt JOIN tags t ON t.id=pt.tag_id
           WHERE pt.photo_id=? AND pt.verified>=0""", (photo_id,))]
    ocr = [r["text"] for r in db.execute(
        "SELECT text FROM ocr_texts WHERE photo_id=?", (photo_id,))]
    geo = db.execute("SELECT * FROM geo WHERE photo_id=?", (photo_id,)).fetchone()
    place = " ".join(filter(None, (
        geo["prefecture"], geo["city"], geo["poi_name"], geo["poi_alt"],
    ))) if geo else ""
    captions = [r["caption_snippet"] for r in db.execute(
        "SELECT caption_snippet FROM photo_posts WHERE photo_id=? AND caption_snippet IS NOT NULL",
        (photo_id,))]

    db.execute("DELETE FROM photos_fts WHERE rowid=?", (photo_id,))
    db.execute(
        "INSERT INTO photos_fts (rowid, tags_text, ocr_text, place_text, caption) "
        "VALUES (?,?,?,?,?)",
        (photo_id,
         tokenize_ja(" ".join(tags)),
         tokenize_ja(" ".join(ocr)),
         tokenize_ja(place),
         tokenize_ja(" ".join(captions))))


def fts_ranks(db: sqlite3.Connection, q: str, limit: int = 200) -> list[int]:
    """BM25 順の photo_id。列重み: タグ 3.0 / OCR 1.0 / 地名 2.0 / caption 1.0"""
    tokens = [t for t in tokenize_ja(q).split() if t.strip()]
    if not tokens:
        return []
    # プレフィックス一致: 「京都」で「京都府」(Sudachi が1トークンにする) もヒットさせる
    match = " OR ".join(f'"{t}"*' for t in tokens)
    try:
        rows = db.execute(
            """SELECT rowid FROM photos_fts WHERE photos_fts MATCH ?
               ORDER BY bm25(photos_fts, 3.0, 1.0, 2.0, 1.0) LIMIT ?""",
            (match, limit)).fetchall()
    except sqlite3.OperationalError:  # クエリ構文に落ちる特殊文字対策
        return []
    return [r["rowid"] for r in rows]
