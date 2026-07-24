# syntax=docker/dockerfile:1
#
# CPU/CUDAの2バリアントを1本のDockerfileから作り分ける (ARG VARIANT=cpu|cuda)。
# メンテナンス性重視の選択: requirements.txt/requirements-gpu.txtの既存の
# 依存分離をそのままベースイメージ選択に対応させ、CPU専用ユーザがCUDA
# ランタイム分のイメージサイズ/脆弱性対応を背負わずに済むようにする
# (docs/docker.md参照)。
ARG VARIANT=cpu

# ---------------------------------------------------------------- base-cpu --
FROM python:3.12-slim AS base-cpu
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-noto-cjk fonts-mplus libgl1 libglib2.0-0 curl tini \
    && rm -rf /var/lib/apt/lists/*

# --------------------------------------------------------------- base-cuda --
# requirements-gpu.txt の動作確認済み組み合わせ (RTX 3060, CUDA 13.3ドライバ,
# onnxruntime-gpu 1.27.0 + nvidia-cudnn-cu13) に合わせた CUDA 13 系ランタイム。
# Ubuntu 24.04 の既定 python3 が 3.12 のため deadsnakes 等は不要
FROM nvidia/cuda:13.0.0-runtime-ubuntu24.04 AS base-cuda
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3-pip \
        fonts-noto-cjk fonts-mplus libgl1 libglib2.0-0 curl tini \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.12 /usr/bin/python3 \
    && ln -sf /usr/bin/python3 /usr/bin/python
# apt由来のpipは "externally-managed-environment" でPEP668保護がかかるが、
# ここは単一用途のコンテナなので割り切って無効化する
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# ------------------------------------------------------------------ runtime -
FROM base-${VARIANT} AS runtime
ARG VARIANT
WORKDIR /app

# UID 10001: nvidia/cuda:*-ubuntu24.04 base already ships a UID-1000 "ubuntu"
# user, so 1000 collides (useradd fails "UID 1000 is not unique") on the cuda
# variant; python:3.12-slim has no such conflict but we use the same UID on
# both variants for consistency
RUN useradd -u 10001 -m -s /usr/sbin/nologin appuser

COPY requirements.txt requirements-gpu.txt ./
# CUDAバリアントはCLAUDE.mdに記録された既存の手順 (無印onnxruntimeと
# onnxruntime-gpuは同居できないため、まず入れてから抜いて差し替える) をそのまま踏む
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$VARIANT" = "cuda" ]; then \
         pip uninstall -y onnxruntime \
         && pip install --no-cache-dir -r requirements-gpu.txt; \
       fi

COPY photofinder/ photofinder/
COPY tools/ tools/
# pillow-heifのLinux wheelはlibheif/libde265に加えlibx265 (GPL-2.0のHEVCエンコーダ、
# デコードのみのPhotoFinderからは実際には呼ばれない) を同梱している (実機確認済み、
# docs/third-party-notices.md参照)。配布物 (このDockerイメージ) にライセンス全文を
# 同梱しておく
COPY docs/third-party-licenses/x265-COPYING.txt THIRD-PARTY-LICENSES/x265-COPYING.txt

ENV PHOTOFINDER_DATA=/app/data \
    PHOTOFINDER_DOCKER=1 \
    PHOTOFINDER_HOST=0.0.0.0 \
    PHOTOFINDER_PORT=8686

# data/・models/はボリューム化 (イメージに焼き込まない)。モデルは別途
# model-fetchステージ/サービスで取得する (docs/docker.md参照)
RUN mkdir -p /app/data /app/models && chown -R appuser:appuser /app
VOLUME ["/app/data", "/app/models"]
EXPOSE 8686

USER appuser

# GET /api/index/status は既存のインデックス状態確認エンドポイントを流用
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f "http://127.0.0.1:${PHOTOFINDER_PORT}/api/index/status" || exit 1

ENTRYPOINT ["tini", "--"]
# --workers 1 は必須: main.pyはモジュールレベルでdb (LockedConnection) と
# vstore (FAISS index) のシングルトンを保持しており、複数ワーカーは
# FAISS/SQLiteの状態競合を起こす (CLAUDE.md「Single process, three stores」参照)
CMD ["sh", "-c", "exec uvicorn photofinder.main:app --host ${PHOTOFINDER_HOST} --port ${PHOTOFINDER_PORT} --workers 1"]

# -------------------------------------------------------------- model-fetch -
# アプリイメージのビルド/再ビルドのたびに ~1.5GB を再取得しないよう、
# モデル取得はランタイムイメージのビルドグラフから独立させている。
# 重要: 実際のダウンロードはこのステージの CMD (コンテナ実行時) が行う —
# Dockerfile の RUN で python tools/download_models.py を実行すると、
# ビルド時にはmodelsボリュームがマウントされていないため、取得結果が
# このステージのイメージ層に焼き込まれてしまい (VOLUME宣言はrun時のみ有効)、
# named volumeには何も残らない。`docker compose --profile setup run --rm
# model-fetch` で「実行」することで初めてボリュームへ正しく書き込まれる
# (docs/docker.md参照)。GPU不要のためbase-cpuから作る (YOLOのONNXエクスポート
# 自体はCPUで完結する)
FROM base-cpu AS model-fetch
WORKDIR /app
RUN pip install --no-cache-dir huggingface_hub ultralytics
COPY tools/download_models.py tools/download_models.py
CMD ["python", "tools/download_models.py"]
