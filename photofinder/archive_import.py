"""X (旧Twitter) データアーカイブ (zip) の取り込み (フェーズ2)。

設定画面からアップロードされた「データのアーカイブ」zipを解析し、
data/tweets.js の投稿一覧 + data/tweets_media/ の実画像を、ローカル写真
ライブラリと SigLIP 埋め込みの類似度で自動照合する。X投稿画像は切り抜き・
透かしでファイルが完全一致しないことが多いため、main.py の重複投稿警告
(_similar_posted_photos) と同じ考え方で意味的類似度を使う。

安全のため自動では photo_posts に書き込まない: 候補一覧を STATUS に貯めて
レビュー画面に提示し、ユーザーが確認したものだけ main.py 側の
POST /api/archive/import/confirm で確定する (誤マッチによる意図しない
リンクを防ぐため)。

投稿数が多いと全件の埋め込み照合に時間がかかる (実測: 2688枚で約16分) ため、
date_from/date_to (投稿日) で対象を絞り込める。2回目以降の取り込みを
期間で区切って少しずつレビューする運用を想定している。

**実アーカイブで検証済み (2026-07-12)**: `window.YTD.tweets.part0 = [...]` プレフィックス、
`{"tweet": {...}}` ラッパー、`id_str`/`created_at`/`full_text`/`extended_entities.media`
(`type`="photo"|"video"|"animated_gif"、`media_url_https`) の形式、`data/account.js` の
`account.username`、`data/tweets_media/{tweet_id}-{media_basename}` というファイル名規則
(media自体のidではなく、それを含むツイート本体のidを使う) をいずれも実物で確認した。
検証に使ったアーカイブ固有の情報 (メールアドレス等の個人情報を含む) は破棄済み。
今後 X 側の書き出し形式が変わった場合は、このファイルの各 `_parse_*` を見直すこと。
"""
from __future__ import annotations

import io
import json
import logging
import re
import threading
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

log = logging.getLogger("photofinder.archive_import")

ARCHIVE_MATCH_MIN_SCORE = 0.75  # main.py の SIMILAR_POST_MIN_SCORE と同じ暫定値


@dataclass
class ArchiveTweet:
    tweet_id: str  # 64bit snowflake ID。JS number 精度落ちを避けるため常に文字列で扱う
    url: str
    posted_at: str | None
    text: str
    media_names: list[str]  # zip 内 data/tweets_media/ 配下の相対ファイル名


@dataclass
class Candidate:
    tweet_id: str
    url: str
    posted_at: str | None
    text: str
    media_name: str
    photo_id: int
    score: float


@dataclass
class ImportStatus:
    running: bool = False
    phase: str = "idle"  # idle | parsing | matching | done | error
    total: int = 0
    done: int = 0
    error: str | None = None
    skipped_existing: int = 0  # 既に photo_posts にリンク済みのURLだったため対象から除外した件数
    skipped_out_of_range: int = 0  # date_from/date_to の指定範囲外だったため対象から除外した件数
    candidates: list[Candidate] = field(default_factory=list)

    def snapshot(self) -> dict:
        return {
            "running": self.running, "phase": self.phase,
            "total": self.total, "done": self.done, "error": self.error,
            "skipped_existing": self.skipped_existing,
            "skipped_out_of_range": self.skipped_out_of_range,
            "candidates": [
                {"tweet_id": c.tweet_id, "url": c.url, "posted_at": c.posted_at,
                 "text": c.text, "photo_id": c.photo_id, "score": round(c.score, 4)}
                for c in self.candidates
            ],
        }


STATUS = ImportStatus()
_lock = threading.Lock()


def try_acquire() -> bool:
    """アップロード受付時に main.py が呼ぶ。取得できたら run_import() の
    finally で解放されるまで保持される。run_import() 自身はロックを取らない
    (アップロードの書き込み中から run_import() 完了まで一つのロックで直列化し、
    別リクエストが書き込み中の一時ファイルへ割り込むのを防ぐため)。"""
    return _lock.acquire(blocking=False)


def release() -> None:
    """try_acquire() 成功後、何らかの理由で run_import() を呼べなかった場合に
    呼び出し側が使う解放関数。通常は run_import() 内の finally が担当する。"""
    _lock.release()


def _strip_ytd_prefix(text: str) -> str:
    """`window.YTD.xxx.partN = [...]` 形式のJSラッパーを剥がしてJSON文字列にする。

    プレフィックスの正確な変数名に依存せず、最初の `[` または `{` から
    末尾までを切り出すことで多少の表記揺れを吸収する。
    """
    m = re.search(r"[\[{]", text)
    if not m:
        raise ValueError("not a YTD-wrapped JS/JSON file")
    return text[m.start():]


def _parse_x_date(s: str | None) -> str | None:
    """Twitter API v1.1 互換の created_at ("Wed Oct 10 20:19:24 +0000 2018") を
    ISO8601 へ変換する。形式が異なれば生の値をそのまま保持する (要目視確認)。"""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y").isoformat()
    except ValueError:
        return s


def _parse_tweets_js(text: str) -> list[ArchiveTweet]:
    data = json.loads(_strip_ytd_prefix(text))
    out = []
    for entry in data:
        t = entry.get("tweet", entry)  # 一部の書き出しは {"tweet": {...}} でラップされる
        tid = t.get("id_str") or t.get("id")
        if not tid:
            continue
        tid = str(tid)
        media = []
        ext_media = ((t.get("extended_entities") or {}).get("media")
                     or (t.get("entities") or {}).get("media") or [])
        for m in ext_media:
            if m.get("type") and m["type"] != "photo":
                continue  # 動画/GIFのサムネイルは画像照合の対象外
            src = m.get("media_url_https") or m.get("media_url") or ""
            fname = src.rsplit("/", 1)[-1]
            if fname:
                media.append(f"{tid}-{fname}")
        if not media:
            continue
        out.append(ArchiveTweet(
            tweet_id=tid,
            url=f"https://x.com/i/status/{tid}",  # ハンドル不明でも機能する汎用URL形式
            posted_at=_parse_x_date(t.get("created_at")),
            text=t.get("full_text") or t.get("text") or "",
            media_names=media,
        ))
    return out


def _parse_account_handle(text: str) -> str | None:
    try:
        data = json.loads(_strip_ytd_prefix(text))
        return (data[0].get("account") or {}).get("username") or None
    except Exception:
        return None


def _find_zip_member(zf: zipfile.ZipFile, *candidates: str) -> str | None:
    names = set(zf.namelist())
    for c in candidates:
        if c in names:
            return c
    # フォルダ構成の揺れ (data/ 直下でない等) に対応してファイル名末尾一致で探す
    tail = candidates[0].rsplit("/", 1)[-1]
    for n in names:
        if n.endswith(tail):
            return n
    return None


def _in_date_range(posted_at: str | None, date_from: str | None, date_to: str | None) -> bool:
    """posted_at (ISO8601) が [date_from, date_to] (YYYY-MM-DD、両端含む) 内かどうか。

    main.py の _build_filters が写真の taken_at で使っているのと同じ流儀
    (date_to は当日いっぱいを含めるため T23:59:59 を付与、文字列の辞書式
    比較で判定) に合わせる。posted_at が None (created_at 欠落など極めて
    まれなケース) の場合、範囲指定があれば安全側に倒して除外する。
    """
    if not date_from and not date_to:
        return True
    if posted_at is None:
        return False
    if date_from and posted_at < date_from:
        return False
    if date_to and posted_at > date_to + "T23:59:59":
        return False
    return True


def run_import(
    zip_path: Path, vstore, existing_urls: set[str],
    date_from: str | None = None, date_to: str | None = None,
) -> None:
    """バックグラウンドスレッドから呼ぶ想定。結果は STATUS に蓄積する。

    date_from/date_to (YYYY-MM-DD、任意・片側のみでも可) を指定すると、
    その期間に投稿されたツイートだけを対象にする。2回目以降の取り込みを
    期間で区切って少しずつレビューしたい、という用途向け
    (全件を毎回埋め込み直すと時間がかかるため)。

    呼び出し前に try_acquire() でロックを取得済みであること。
    """
    STATUS.__init__()  # 前回分をリセット (scanner.STATUS と同じ流儀)
    STATUS.running, STATUS.phase = True, "parsing"
    try:
        from . import ml  # 遅延import (main.py と同じ理由: モデル未導入でも他機能に影響させない)
        with zipfile.ZipFile(zip_path) as zf:
            tweets_name = _find_zip_member(zf, "data/tweets.js", "data/tweet.js")
            if not tweets_name:
                raise ValueError(
                    "tweets.js が見つかりません (アーカイブの形式が想定と異なる可能性があります)")
            tweets = _parse_tweets_js(zf.read(tweets_name).decode("utf-8"))

            account_name = _find_zip_member(zf, "data/account.js")
            handle = _parse_account_handle(zf.read(account_name).decode("utf-8")) \
                if account_name else None
            if handle:
                for t in tweets:
                    t.url = f"https://x.com/{handle}/status/{t.tweet_id}"

            before = len(tweets)
            tweets = [t for t in tweets if t.url not in existing_urls]
            STATUS.skipped_existing = before - len(tweets)

            before = len(tweets)
            tweets = [t for t in tweets if _in_date_range(t.posted_at, date_from, date_to)]
            STATUS.skipped_out_of_range = before - len(tweets)

            media_items = [(t, m) for t in tweets for m in t.media_names]
            STATUS.total = len(media_items)
            STATUS.phase = "matching"

            for t, media_name in media_items:
                STATUS.done += 1
                if not ml.runtime.available:
                    continue
                member = _find_zip_member(zf, f"data/tweets_media/{media_name}")
                if not member:
                    continue
                try:
                    img = Image.open(io.BytesIO(zf.read(member)))
                    img = ImageOps.exif_transpose(img).convert("RGB")
                except Exception:
                    continue  # 壊れた/非対応形式の画像はスキップ
                vec = ml.runtime.siglip_image(img)
                for photo_id, score in vstore.search(vec, k=5):
                    if score < ARCHIVE_MATCH_MIN_SCORE:
                        continue
                    STATUS.candidates.append(Candidate(
                        tweet_id=t.tweet_id, url=t.url, posted_at=t.posted_at,
                        text=t.text, media_name=media_name,
                        photo_id=photo_id, score=score))
                    break  # 画像1枚につきベスト1候補のみ (多重候補を避ける)
        STATUS.phase = "done"
    except Exception as e:
        log.exception("archive import failed")
        STATUS.error = str(e)
        STATUS.phase = "error"
    finally:
        STATUS.running = False
        _lock.release()
