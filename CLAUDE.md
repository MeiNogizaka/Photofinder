# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

PhotoFinder: a local-only photo search app for personal use, forked from `app2/photofinder`
(kept unchanged as the fork point). Natural-language (Japanese) and image-similarity search over
photos on disk, with EXIF, OCR, object detection, bird species ID, and place-name enrichment — all
computed once at index time, served from a single FastAPI process. No cloud services by default;
binds to `127.0.0.1` only by default. See [README.md](README.md) for the full feature list and
design rationale.

**What changed from app2/photofinder**: distribution moved from a Windows PyInstaller exe to
**Docker only** (CPU and CUDA image variants from one `Dockerfile`, see [docs/docker.md](docs/docker.md));
added **RAW file support** (`photofinder/raw_utils.py`, embedded-preview extraction via `rawpy`,
previously unimplemented); tuned **FAISS/RRF search** (HNSW `efConstruction`/`efSearch`, dynamic
top-k widening — implements the design intent that `docs/design.md` §9.2 always described but the
old `main.py` never did); added a **dataset export** endpoint (`POST /api/export/dataset`,
`photofinder/dataset_export.py`) that turns human-confirmed/rejected tags into a training/eval
JSONL+image zip. DirectML (Windows GPU/APU) support was removed entirely — Windows is no longer a
target platform at all, not even for local dev.

**License: AGPL-3.0** (see [LICENSE](LICENSE)) — forced by the bundled YOLOv8x object-detection
model, which Ultralytics licenses as AGPL-3.0. Most of the rest of the stack is permissively
licensed (MIT/BSD/Apache-2.0) or LGPL (`reverse_geocoder`, and now `rawpy`'s bundled `LibRaw`),
which is compatible with/subsumed by AGPL. `pillow-heif`'s Linux wheel bundles a GPL-2.0 `libx265.so`
(unused at runtime — decode-only — but still shipped; its `COPYING` is bundled into the Docker image,
see the Dockerfile). Don't assume "add any dependency, it's all permissive here" — see
[docs/third-party-notices.md](docs/third-party-notices.md) before adding any new dependency, model,
or dataset.

## Commands

```bash
# Docker (the actual distribution path — see docs/docker.md for full detail)
cp .env.example .env   # set PHOTO_LIBRARY_PATH
docker compose --profile setup run --rm model-fetch   # once, ~1.5GB
docker compose --profile cpu up -d     # or --profile cuda (needs nvidia-container-toolkit)

# Local dev without Docker (Linux only — Windows is not a supported target at all anymore)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install huggingface_hub ultralytics   # temp, only for the YOLOv8x ONNX export below
.venv/bin/python tools/download_models.py            # SigLIP2/YOLOv8x/OCR ONNX models -> models/ (~1.5GB)
.venv/bin/pip uninstall ultralytics torch torchvision -y

# optional: CUDA acceleration in local dev (mirrors the Docker cuda variant)
.venv/bin/pip uninstall onnxruntime -y
.venv/bin/pip install -r requirements-gpu.txt        # onnxruntime-gpu + nvidia-cudnn-cu13

.venv/bin/python -m uvicorn photofinder.main:app --port 8686

# Generate synthetic test photos (EXIF/GPS) or fetch real ones for manual testing
.venv/bin/python tools/make_sample_photos.py
.venv/bin/python tools/fetch_test_photos.py

# Fetch OSM POI data for a prefecture (also doable from the Settings UI)
.venv/bin/python tools/build_poi_db.py 京都府 大阪府

# Evaluate AI auto-tag accuracy against human confirm/reject decisions (verified!=0).
# Takes the zip from POST /api/export/dataset (also downloadable from Settings UI)
.venv/bin/python tools/evaluate_dataset.py dataset-20260725-120000.zip
```

There is no automated test suite (no pytest config, no `tests/` directory). Verification is done by
running the live dev server (or a running Docker container) and driving it through its HTTP API /
browser UI — write a throwaway script that calls the endpoints under test (scan a folder, check
`GET /api/index/status`, query `GET /api/search`, inspect the SQLite DB directly, etc.), run it, then
discard it. When changing `scanner.py`'s diff-scan logic in particular, test with real file
operations (move/rename/overwrite-in-place) against actual folders, not just unit-level assertions —
the move/tombstone edge cases only surface with real filesystem timing and content hashes.

## Architecture

**Single process, three stores.** One FastAPI process (`photofinder/main.py`) holds everything:
SQLite (WAL mode, `photofinder/schema.sql` is the sole DDL source of truth — never duplicate DDL
into docs) for metadata + FTS5 full-text, FAISS (`IndexHNSWFlat`, file-backed) for embedding vectors,
and the filesystem for WebP thumbnails/previews. FAISS vector IDs are `photos.id` directly — no
separate mapping table. A second, independent SQLite file (`data/poi.db`) holds OSM POI data and is
never joined against the main DB; `geo.py` queries it directly at request time. **`--workers 1` is
mandatory** for any deployment (Docker `CMD` already enforces this) — `db` and `vstore` are
module-level singletons in `main.py`; multiple uvicorn workers would each hold independent
FAISS/SQLite state and race on writes.

**FAISS's C++ file I/O is not Unicode-path-safe on Windows.** This was found on real hardware while
app2/photofinder still targeted Windows (non-ASCII paths like Japanese "デスクトップ" broke
`faiss.read_index`/`write_index`). photofinder has no Windows target at all, so the trigger no
longer applies, but `photofinder/vectors.py`'s `_read_index`/`_write_index` helpers (which route
through Python's own `open()` + `PyCallbackIOReader`/`Writer` instead of a raw path string) are kept
as-is since they're harmless and correct on Linux too — never call `faiss.read_index`/`write_index`
with a raw path for any future FAISS file I/O added here.

**Diff-based incremental scanner** (`photofinder/scanner.py`): walks each registered root, compares
`(size, mtime)` against the DB to skip unchanged files cheaply, and only computes an xxHash (head+tail
1MB) when something looks different. A hash match at a new path is treated as a move/rename (record
reused, no re-extraction); a hash match at an already-occupied path purges the occupant first (avoids
a `UNIQUE(root_id, path)` collision). Deleted files are soft-deleted (`photos.deleted=1`, "tombstone")
rather than removed, and FTS/R-tree side rows are cleaned up explicitly at tombstone time (no FK
CASCADE on SQLite virtual tables). `photos.ml_version` records which pipeline version last processed
a photo, so bumping `ML_VERSION` in `scanner.py` triggers automatic backfill on the next scan. Extract
failures are rolled back and marked `index_state='error'` for retry, not left half-committed.

**RAW support (`photofinder/raw_utils.py`) reuses the shared working-image pipeline, not a parallel
one.** `scanner.py`'s `_open_working_image(path)` is the single decision point for "how do I get a
decodable PIL Image from this file" — it branches on `raw_utils.is_raw(ext)` (a curated default set,
`RAW_EXTS`, not a hard allowlist; `roots.ext_filter` is free-text so users can add more) and both
`extract_one()` and `get_or_make_preview()` (previously two independent `Image.open()` call sites)
now go through it. `raw_utils.load_raw_preview()` uses `rawpy`'s `extract_thumb()` — the camera's
own embedded JPEG preview, **not a full demosaic** — falling back to a `half_size` postprocess only
when no embedded preview exists. `exif_utils.py` tries `piexif` first (works for some TIFF-based RAW
containers), then falls back to `exifread` only when the file `is_raw()` and piexif returned nothing;
CR3 (ISO-BMFF/QuickTime container, unlike CR2's TIFF) may still come back with empty EXIF even though
the image itself decodes fine — a known, accepted gap. `photos.width`/`height` for RAW files reflect
the *embedded preview's* resolution, not the sensor's native resolution — documented in `schema.sql`.
`photofinder/export.py`'s watermark/crop export flow got the same RAW branch (`raw_utils` import) so
exporting a RAW-sourced photo doesn't hit the old `Image.open()`-only path and crash.

**Search is RRF fusion**, not a single index: FTS5 (BM25 over SudachiPy-tokenized Japanese text) and
FAISS (SigLIP text-embedding cosine similarity) are ranked independently, then merged by reciprocal
rank fusion (`RRF_K=60`). Non-empty `q` always uses this hybrid path; sort order (`order=asc|desc`)
only applies when browsing with an empty query, since RRF ranking otherwise takes priority. Within
that browse path, the sort *column* itself switches too: `exported=true` (see below) sorts by
`photos.exported_at` instead of `taken_at`, so "browse exported photos" defaults to most-recently-
exported-first rather than most-recently-taken-first.
**Top-k is now dynamic** (`main.py`'s `_widen_k()`): app2/photofinder hardcoded `k=200` for the FAISS
leg regardless of filters, even though `docs/design.md` always described widening it based on filter
selectivity (`k=widen(limit, filters)` in the §9.2 pseudocode) — that gap is now closed. Selectivity
is estimated with one cheap `COUNT(*)` reusing the same `conds`/`args` `_build_filters()` already
produces; narrower filters get a wider `k` (200→500→1000→2000) so a tight filter combo doesn't starve
the vector leg of candidates. `vectors.py`'s `search()` derives `efSearch` from that same `k`
(`max(128, min(4000, k*2))`) on every call, and `efConstruction=200` is set at index-construction time
(FAISS's default of 40 was never tuned before). `search_by_image`'s near-duplicate lookup (fixed
`k=50`) intentionally does **not** get the widening — different UX (finding a specific original), not
filtered browsing.

**ML is all ONNX, GPU-accelerated where available (CUDA only now), zero-shot where possible.**
`photofinder/ml.py` holds the SigLIP2 session (shared by image and text encoding, 1152d, 384px,
`google/siglip2-so400m-patch14-384`, WebLI 109-language training covers Japanese); `detector.py` is
YOLOv8x for object tags; bird species (`bird.py`) and color tags (`colors.py`) are *not* trained
classifiers — they're SigLIP2 zero-shot similarity against a curated text list (`species_ja.py`),
because off-the-shelf bird classifiers don't cover common Japanese species (see `bird.py`'s docstring
for the calibration caveats — `MIN_SIM`/`MIN_MARGIN` were hand-tuned on a very small real-photo
sample and explicitly flagged as needing real-world monitoring). Every ML module follows the same
lazy-load + `available` property pattern (see `ocr.py`'s `OCREngine`) so a missing model file
degrades that one feature instead of crashing the app — preserve this pattern when adding new ML
features.

**Embedding dimension changes force a vector-store rebuild, not just a model swap.** `ml.DIM` (1152)
flows into `VectorStore`'s FAISS index dimensionality (`vectors.py`). `VectorStore.__init__` checks
the loaded index's actual dimension against `ml.DIM` and silently discards+rebuilds an empty index
on mismatch (logged as a warning). The zero-shot banks in `bird.py`/`colors.py`
(`species_bank.npz`/`color_bank.npz`) mix `ml.DIM` into their own cache-invalidation hash too. Any
future embedding-model swap that changes `DIM` needs `scanner.ML_VERSION` bumped too (so the backfill
mechanism re-encodes every photo).

**GPU acceleration: CUDA only, no DirectML.** `ml.py`'s `ORT_PROVIDERS = ["CUDAExecutionProvider",
"CPUExecutionProvider"]` is reused by every ONNX session in the app (`ml.py`, `detector.py`).
DirectML (Windows GPU/APU support) existed in app2/photofinder, crashed on real RTX 3060 hardware
there, and has been removed entirely here since Docker/Linux is now the only target — there is no
DirectML code path to accidentally re-enable. Requesting an unavailable provider (CUDA package not
installed, or no GPU) never raises — `InferenceSession` just falls back to CPU with a stderr warning
(verified on a GPU-less VM). Each session's actual active provider is queryable via
`.get_providers()[0]` after first load (`MLRuntime.text_provider`/`.vision_provider`,
`Detector.active_provider`) and surfaced in `GET /api/index/status`'s `providers` field — since every
model is lazy-loaded, a model's provider reads `null` until its first real use.

**`_add_pip_cuda_lib_paths()` (`ml.py`) must handle a totally absent `nvidia` namespace package,
not just a missing submodule.** `importlib.util.find_spec("nvidia.cudnn")` raises
`ModuleNotFoundError` (not "returns None") when the top-level `nvidia` package isn't installed at
all — confirmed by an actual crash when this was first run in a clean Docker CPU image (no
`nvidia-*` packages whatsoever). app2/photofinder's dev venvs likely never hit this because a
leftover `nvidia-*` install (from the temporary `pip install ultralytics`→torch step during model
download) masked it. The fix wraps the `find_spec` call in `try/except ModuleNotFoundError: continue`
— don't remove that guard, a genuinely clean CPU install (exactly what the Docker CPU image is) will
hit it immediately on import.

**A plain `LD_LIBRARY_PATH` env-var write from inside the running process does not work** for the
CUDA cuDNN preload — glibc's dynamic linker reads `LD_LIBRARY_PATH` once at process startup.
`ml.py`'s `_add_pip_cuda_lib_paths()` uses `ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)` to explicitly
preload every `nvidia-cudnn-cu13` `.so` into the process *before* onnxruntime is imported, so
onnxruntime's later `dlopen(..., "libcudnn.so.9")` resolves against the already-loaded library. This
works identically inside the `nvidia/cuda` Docker base image — the root cause is a pip-wheel `.so`
path issue, not an OS-level cuDNN absence, so the container's CUDA runtime being present doesn't make
this preload step unnecessary.

**SNS post links reuse the embedding index, not a new matcher.**
`photo_posts` (schema.sql) stores manual links between a local photo and a post URL, tagged with
`platform` (`x`/`instagram`/`other`, schema v6+; `source` is a separate manual/archive-import axis,
not the platform). Because posted images are often crops/rewatermarks of the local original,
duplicate-post warnings (`GET /photos/{id}/posts/similar`, `main.py`) match on SigLIP cosine
similarity via `VectorStore.get_vector()` + `.search()` rather than xxhash/pHash exact matching,
which doesn't survive a crop — this check is platform-agnostic (any linked post counts, regardless
of `platform`). `SIMILAR_POST_MIN_SCORE` (0.75) is an untuned starting heuristic. Adding a post URL
with `platform=="x"` (the default) triggers a best-effort network call to X's public oEmbed endpoint
for a caption snippet — the one exception to "no cloud services by default" in this codebase, and it
degrades silently (empty snippet) rather than failing the request; non-X platforms skip this call
entirely rather than let it fail against a URL it was never meant to resolve.

**Content-addressed cache URLs.** `/api/photos/{id}/thumb` and `/preview` are served with a long
`Cache-Control: immutable` header, but since a `photo_id` can legitimately end up pointing at
different bytes over time (e.g. a file overwritten in place at the same path), the URL includes
`?h={xxhash}` so the browser cache key changes whenever the content does. Follow this pattern for
any new long-cached per-photo asset — don't cache-bust by `photo_id` alone.

**Dataset export (`photofinder/dataset_export.py`, `POST /api/export/dataset`) treats
`photo_tags.verified != 0` as ground truth — and specifically NOT `bird_ids.confirmed`.**
`bird_ids.confirmed` is a separate column from `photo_tags.verified` that the existing ✓/✗ UI action
(`main.py`'s `verify_tag()`) never updates — it's set once at INSERT time in `extract_one()` and
always stays `0` afterward. Gating species detail on `bird_ids.confirmed` would silently produce
meaningless (always-empty) data; the export instead joins `detections`→`bird_ids` and gates on the
corresponding `photo_tags.verified` value, matched by species tag name. `verified=1` rows become
`positive_tags`, `verified=-1` (explicitly human-rejected AI suggestions) become `negative_tags` by
default — these are valuable for recalibrating `bird.py`/`colors.py`'s `MIN_SIM`/`MIN_MARGIN`
thresholds. Exported images are the existing 1600px preview WebP (`scanner.get_or_make_preview()`),
not the original — well above SigLIP/YOLO's input resolution, and consistent with the existing
GPS/EXIF-stripping export philosophy.

**`IN_DOCKER` (`main.py`, from `PHOTOFINDER_DOCKER` env var, set by the Dockerfile) gates the
"open in file manager" endpoint.** A container has no desktop to open a file manager on, so
`POST /photos/{id}/open-in-explorer` returns 501 when `IN_DOCKER` is true, and the frontend hides
the corresponding button based on `GET /api/index/status`'s `in_docker` field. The Windows-specific
`_reveal_windows()` Win32 shell API implementation from app2/photofinder was deleted outright (not
just gated) — Windows isn't a target platform here even for local dev, so it was dead code from day
one in this repo.

**Exports never persist inside the container — both `POST /photos/{id}/export` and
`POST /api/export/dataset` stream straight back to the browser.** This replaced an earlier design
(app2/photofinder-era) that wrote into `data/exports/` and returned a container-local path, plus a
now-deleted `POST /api/reveal` endpoint to open that folder — useless the moment distribution moved
to Docker-only, since a container has nothing to reveal a folder *in*. `export.export_photo()` now
returns `(bytes, filename)` built entirely in memory (no `out_dir` param, no on-disk collision-avoidance
loop) and `main.py`'s `export()` wraps that in a `Response` with `Content-Disposition: attachment`.
The dataset zip (`export_dataset()`) is unavoidably file-backed (`zipfile.ZipFile` needs a path, and
builds can run to thousands of photos) but uses `tempfile.mkstemp()` — outside the `data/` named
volume entirely — and deletes it via a `BackgroundTask` attached to the `FileResponse`, after the
response finishes streaming. Follow this pattern (in-memory bytes, or a temp file + `BackgroundTask`
cleanup) for any future export-like endpoint — never write a new one into `data/exports/`. `main.py`'s
`export()` does write one thing to SQLite on success: `photos.exported_at` (schema v6+), a timestamp
used to show an "already exported" badge in the grid (`_hydrate()`'s `exported` field) and, via
`GET /api/search`'s `exported=true|false` param (`_build_filters()`), to let a user browse only
exported/unexported photos — sorted by `exported_at` itself (most recent first by default) rather
than `taken_at` when that filter is active and browsing with no `q`, mirroring the `posted` filter's
existing `photo_posts`-existence check but querying a plain column instead of a join. This is
metadata about the export having happened, not the exported bytes themselves, so it doesn't reintroduce
on-disk persistence of the export output.

**Docker packaging** (`Dockerfile` + `docker-compose.yml`, see [docs/docker.md](docs/docker.md) for
the full rationale and troubleshooting): one `Dockerfile`, `ARG VARIANT=cpu|cuda` selects the base
image and whether `requirements-gpu.txt` gets layered on top of `requirements.txt`. Model download
(`tools/download_models.py`, ~1.5GB, needs a temporary `ultralytics`/torch install for the YOLOv8x
ONNX export) is a **separate build target and a separate `docker compose run`** — critically, the
actual download must happen as the `model-fetch` service's container *command* at `docker run` time,
not as a Dockerfile `RUN` step, since `docker build` doesn't mount volumes; baking it into a `RUN`
would write into that build stage's image layer instead of the `models` named volume, and the volume
would end up empty. `data/` and `models/` are both named volumes, never baked into the image. The
photo library is a read-only bind mount at `/photos` — users must register scan roots using the
*container* path (`/photos/...`) in the UI, not the host path.

**`docker-compose.yml`'s `photofinder-cpu`/`photofinder-cuda` services must set `build.target:
runtime` explicitly — omitting it silently builds the wrong stage.** Confirmed by an actual repro:
without `target: runtime`, `docker compose --profile cuda up` built and ran the Dockerfile's *last*
stage (`model-fetch`, since Docker builds the last stage of a multi-stage file when no target is
given) instead of `runtime`. The container came up tagged `photofinder:cuda` but its `CMD` was
`python tools/download_models.py` — it printed "exists: ..." for every model (already present from
the earlier `model-fetch` run), exited 0, and `restart: unless-stopped` then restart-looped it
forever. No error surfaced anywhere; the only symptom was the container never serving HTTP and
`docker ps` showing "Restarting". Both app services set `target: runtime` for exactly this reason —
don't remove it, and if you add a new service built from this Dockerfile, set an explicit `target:`
too rather than relying on the default.

**Frontend is one static HTML file** (`photofinder/static/index.html`) — vanilla JS, no build step,
no framework. `samples/typescript/` has an unfinished React reference implementation that is *not*
wired up to the current API; don't assume it reflects current endpoints.

## Where to look for more detail

- [docs/design.md](docs/design.md) — original design doc (inherited from app2/photofinder); the
  table at the top lists every place the app2/photofinder implementation diverged from it, and a
  new §11 at the end lists what photofinder changed further.
- [docs/api-spec.md](docs/api-spec.md) — REST endpoint reference.
- [docs/data-schema.md](docs/data-schema.md) — ER diagram and the `poi.db`/FAISS file formats.
- [docs/docker.md](docs/docker.md) — Docker build/deploy details, volume layout, network exposure,
  troubleshooting (this replaces the old `docs/exe-packaging.md`, which no longer applies).
