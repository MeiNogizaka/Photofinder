"""ML ランタイム (M2): SigLIP2 多言語埋め込み + pHash。

モデル: onnx-community/siglip2-so400m-patch14-384-ONNX (int8 量子化)
  - models/siglip/text_model_quantized.onnx   : 多言語(109言語, 日本語含む)テキスト → 1152d
  - models/siglip/vision_model_quantized.onnx : 画像 (384x384) → 1152d
どちらも pooler_output が埋め込み。L2 正規化して内積 = コサイン類似度。
入出力テンソル名は先代の SigLIP (base) と同一 (input_ids/pixel_values/pooler_output) だが、
次元が 768→1152、画像解像度が 256→384 に変わっているため、
既存の data/vectors.faiss (768次元で構築済み) とは非互換。DIM変更時は
VectorStore 側の次元不一致チェックで自動的に索引を作り直す (vectors.py 参照)。
scanner.ML_VERSION を上げてバックフィルすることで全写真の埋め込み再計算を促す。
"""
from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

import numpy as np
from PIL import Image

from .paths import app_root

MODELS_DIR = app_root() / "models" / "siglip"
IMAGE_SIZE = 384
TEXT_MAX_LEN = 64
DIM = 1152


def _add_pip_cuda_lib_paths() -> None:
    """pip 版 nvidia-cudnn-cu13 等 (requirements-gpu.txt) の .so を事前ロードする。

    onnxruntime-gpu の CUDAExecutionProvider は libcudnn.so.9 を dlopen できないと
    例外にならず黙って CPU にフォールバックする (RTX 3060 実機で確認済み: エラーは
    stderr に出るが InferenceSession 自体は成功し、get_providers()[0] が
    'CPUExecutionProvider' になるだけ)。pip 版 nvidia-cudnn-cu13 はサイトパッケージ
    配下に .so を置くだけで通常のライブラリ検索パスに入らない。

    os.environ["LD_LIBRARY_PATH"] を書き換えるだけでは効かない (実機で確認済み: glibc の
    動的リンカはプロセス起動時に一度だけ LD_LIBRARY_PATH を読むため、起動後に Python
    から書き換えても後続の dlopen には反映されない) ため、ctypes.CDLL(..., RTLD_GLOBAL)
    で該当 .so を明示的にプロセスへロードしておく。一度ロードされていれば、
    onnxruntime 側が soname (libcudnn.so.9) で dlopen する際にプロセス内の
    ロード済みライブラリとして解決される。system 側に既に cuDNN がある/パッケージ
    未導入の環境では何もしない。onnxruntime を import する前に (どのモジュールからでも)
    呼ぶ必要があるため、ml.py の import 時点で1度だけ実行する。
    """
    import ctypes

    for pkg in ("cudnn", "cublas", "cuda_runtime"):
        try:
            # 親パッケージ nvidia 自体が一切未インストールだと (CPUイメージの
            # ようにnvidia-*系パッケージがまったく無い環境)、find_spec は
            # 存在しないサブモジュールに対してNoneを返すのではなく
            # ModuleNotFoundErrorを送出する (Dockerの素のCPUイメージで実機確認)。
            # 旧app2/photofinderの開発機では ultralytics の一時インストール
            # (torch経由でnvidia-*が転送インストールされる) の残留物により
            # このケースが顕在化していなかったとみられる
            spec = importlib.util.find_spec(f"nvidia.{pkg}")
        except ModuleNotFoundError:
            continue
        if not spec or not spec.submodule_search_locations:
            continue
        lib_dir = Path(list(spec.submodule_search_locations)[0]) / "lib"
        if not lib_dir.is_dir():
            continue
        for so in sorted(lib_dir.glob("lib*.so.*")):
            try:
                ctypes.CDLL(str(so), mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass


_add_pip_cuda_lib_paths()

# 優先順位: CUDA (Docker cudaバリアント, requirements-gpu.txt) → CPU。
# CUDAExecutionProvider は対応するonnxruntime-gpu/cuDNNが無い環境では単に無視されて
# CPUEPにフォールバックするだけで例外にはならない (GPU無しVMで実機確認済み)。
# GPU高速化したい場合は Docker cudaバリアント (requirements-gpu.txt, onnxruntime-gpu
# + nvidia-cudnn-cu13) を使う。DirectML(Windows)対応コードはDocker専用配布への
# 移行に伴い廃止した (旧app2/photofinderではRTX 3060実機でクラッシュが確認され
# CPU固定運用だった経緯がある)。
ORT_PROVIDERS = ["CUDAExecutionProvider", "CPUExecutionProvider"]


def session_provider(session) -> str | None:
    """ONNX セッションが実際に使っている実行プロバイダ。未ロード (None) なら None。

    GET /api/index/status の providers 表示用。ml.py / detector.py の
    全セッションでこの1関数を共有する (「未ロード → None」の規約の一元化)。
    """
    return session.get_providers()[0] if session is not None else None


class MLRuntime:
    """ONNX セッションを遅延ロードで保持。プロセスに1つ。スレッドセーフ。"""

    def __init__(self, models_dir: Path = MODELS_DIR):
        self.models_dir = models_dir
        self._lock = threading.Lock()
        self._text = None
        self._vision = None
        self._tokenizer = None

    @property
    def available(self) -> bool:
        # テキスト側だけ揃っていても siglip_image() (スキャン抽出・画像類似検索)
        # は vision モデルが無いと使えないため、_load_text/_load_vision が実際に
        # 読む3ファイルすべてが揃って初めて available とする (tokenizer.json 欠如は
        # available=True のまま _load_text() で例外になり得るため対象に含める)
        return (
            (self.models_dir / "text_model_quantized.onnx").exists()
            and (self.models_dir / "vision_model_quantized.onnx").exists()
            and (self.models_dir / "tokenizer.json").exists()
        )

    @property
    def text_provider(self) -> str | None:
        return session_provider(self._text)

    @property
    def vision_provider(self) -> str | None:
        return session_provider(self._vision)

    # ---------------------------------------------------------- loaders ----

    def _load_text(self):
        with self._lock:
            if self._text is None:
                import onnxruntime as ort
                from tokenizers import Tokenizer
                self._text = ort.InferenceSession(
                    str(self.models_dir / "text_model_quantized.onnx"),
                    providers=ORT_PROVIDERS)
                tok = Tokenizer.from_file(str(self.models_dir / "tokenizer.json"))
                pad_id = tok.token_to_id("</s>")
                if pad_id is None:
                    pad_id = tok.token_to_id("<pad>") or 0
                tok.enable_truncation(max_length=TEXT_MAX_LEN)
                tok.enable_padding(length=TEXT_MAX_LEN, pad_id=pad_id)
                self._tokenizer = tok
        return self._text, self._tokenizer

    def _load_vision(self):
        with self._lock:
            if self._vision is None:
                import onnxruntime as ort
                self._vision = ort.InferenceSession(
                    str(self.models_dir / "vision_model_quantized.onnx"),
                    providers=ORT_PROVIDERS)
        return self._vision

    # --------------------------------------------------------- encoders ----

    def siglip_text(self, text: str) -> np.ndarray:
        """多言語テキスト (日本語含む) → 1152d (L2 正規化済み)"""
        session, tok = self._load_text()
        ids = np.array([tok.encode(text.lower()).ids], dtype=np.int64)
        out = session.run(["pooler_output"], {"input_ids": ids})[0][0]
        return _l2(out)

    def siglip_image(self, img: Image.Image) -> np.ndarray:
        """PIL 画像 → 1152d (L2 正規化済み)"""
        session = self._load_vision()
        # preprocessor_config.json (resample=2=bilinear) に合わせる
        x = img.convert("RGB").resize((IMAGE_SIZE, IMAGE_SIZE), Image.BILINEAR)
        arr = np.asarray(x, dtype=np.float32) / 255.0        # rescale
        arr = (arr - 0.5) / 0.5                              # normalize mean/std 0.5
        arr = arr.transpose(2, 0, 1)[None]                   # NCHW
        out = session.run(["pooler_output"], {"pixel_values": arr})[0][0]
        return _l2(out)


def _l2(v: np.ndarray) -> np.ndarray:
    v = v.astype(np.float32)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


# ------------------------------------------------------------- pHash -------

_DCT32 = None


def _dct_matrix(n: int = 32) -> np.ndarray:
    global _DCT32
    if _DCT32 is None:
        k, i = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
        m = np.cos(np.pi / n * (i + 0.5) * k) * np.sqrt(2.0 / n)
        m[0] /= np.sqrt(2.0)
        _DCT32 = m.astype(np.float64)
    return _DCT32


def phash64(img: Image.Image) -> bytes:
    """64bit perceptual hash。SNS 再圧縮・縮小に頑健な同一画像判定用。"""
    g = np.asarray(img.convert("L").resize((32, 32), Image.LANCZOS), dtype=np.float64)
    m = _dct_matrix(32)
    dct = m @ g @ m.T
    low = dct[:8, :8].flatten()
    med = np.median(low[1:])                                 # DC 成分除外で中央値
    bits = (low > med).astype(np.uint8)
    return np.packbits(bits).tobytes()


def hamming(a: bytes, b: bytes) -> int:
    return bin(int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).count("1")


runtime = MLRuntime()
