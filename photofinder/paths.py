"""アプリのルートパス解決 (Docker コンテナ実行前提)。

外部データ vs 同梱データの使い分け:
- モデル・DB・キャッシュ等、ユーザ環境ごとに異なる/差し替え可能なファイルは
  ここの app_root()/data_dir() 基準。Docker では data_dir() を PHOTOFINDER_DATA
  経由でボリュームマウントに向ける。
- schema.sql や static/ のようにアプリコードと不可分の同梱データは、
  従来通り Path(__file__).parent 基準のままにする (db.py の SCHEMA_PATH,
  main.py の STATIC_DIR)。
"""
from __future__ import annotations

import os
from pathlib import Path


def app_root() -> Path:
    return Path(__file__).parent.parent


def data_dir() -> Path:
    """外部データ (DB・サムネ・FAISS・キャッシュ・POI等) のルート。

    PHOTOFINDER_DATA 環境変数があればそれを優先する (テスト時のデータ分離用)。
    未設定なら app_root()/data。main.DATA_DIR だけでなく bird/colors の
    キャッシュ・POI_DB もこれを基準にすることで、環境変数指定時に一部だけ
    旧データを見てしまう不整合を防ぐ。
    """
    override = os.environ.get("PHOTOFINDER_DATA")
    return Path(override) if override else app_root() / "data"
