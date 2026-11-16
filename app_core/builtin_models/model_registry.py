"""Lazy, CPU-only registry for bundled lightweight models.

Runtime dependencies are ``onnxruntime`` + ``tokenizers`` + ``numpy`` and
``spaCy``.  No PyTorch is imported or required.  Model binaries are placed in
``workspace/models`` (or downloaded from a configured URL with checksum
verification); the application degrades gracefully when a model is absent.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

log = logging.getLogger(__name__)

ProgressCallback = Callable[[dict[str, Any]], None]
MIRROR_BASES = ("https://hf-mirror.com", "https://huggingface.co")


class ModelUnavailable(RuntimeError):
    pass


@dataclass
class ModelSpec:
    name: str
    kind: str  # spacy | onnx_embedding | onnx_nli | onnx_reranker
    filename: str = ""
    tokenizer_filename: str = ""
    dimension: int = 0
    package: str = ""
    url: str = ""
    sha256: str = ""
    description: str = ""
    labels: list[str] = field(default_factory=list)
    repo_id: str = ""
    remote_filename: str = ""
    tokenizer_remote_filename: str = "tokenizer.json"
    min_bytes: int = 1_000_000
    approx_bytes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


BUILTIN_MODELS: dict[str, ModelSpec] = {
    "spacy_en": ModelSpec(
        name="spacy_en",
        kind="spacy",
        package="en_core_web_sm",
        description="句法分析、分句与条件从句保护",
    ),
    "mini_cluster_embed": ModelSpec(
        name="mini_cluster_embed",
        kind="onnx_embedding",
        filename="all-MiniLM-L6-v2-int8.onnx",
        tokenizer_filename="all-MiniLM-L6-v2-tokenizer.json",
        dimension=384,
        description="同义结论图聚类（384 维，仅内存，不入主向量库）",
        repo_id="Xenova/all-MiniLM-L6-v2",
        remote_filename="onnx/model_quantized.onnx",
        min_bytes=5_000_000,
        approx_bytes=23_000_000,
    ),
    "conflict_nli": ModelSpec(
        name="conflict_nli",
        kind="onnx_nli",
        filename="nli-deberta-v3-xsmall-int8.onnx",
        tokenizer_filename="nli-deberta-v3-xsmall-tokenizer.json",
        labels=["contradiction", "neutral", "entailment"],
        description="结论极性冲突检测（NLI）",
        repo_id="Xenova/nli-deberta-v3-xsmall",
        remote_filename="onnx/model_quantized.onnx",
        min_bytes=5_000_000,
        approx_bytes=80_000_000,
    ),
    "local_reranker": ModelSpec(
        name="local_reranker",
        kind="onnx_reranker",
        filename="ms-marco-MiniLM-L-6-v2-int8.onnx",
        tokenizer_filename="ms-marco-MiniLM-L-6-v2-tokenizer.json",
        description="本地交叉编码器精排",
        repo_id="Xenova/ms-marco-MiniLM-L-6-v2",
        remote_filename="onnx/model_quantized.onnx",
        min_bytes=5_000_000,
        approx_bytes=23_000_000,
    ),
}


class ModelRegistry:
    """Singleton-style lazy loader; instantiate once per application."""

    def __init__(self, workspace_dir: str | Path):
        self.workspace = Path(workspace_dir)
        self.model_dir = self.workspace / "models"
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self._spacy: Any = None
        self._spacy_tried = False
        self._onnx_cache: dict[str, Any] = {}
        self._tokenizer_cache: dict[str, Any] = {}

    # ------------------------------------------------------------------ specs
    def spec(self, name: str) -> ModelSpec:
        if name not in BUILTIN_MODELS:
            raise KeyError(f"未知内置模型: {name}")
        return BUILTIN_MODELS[name]

    def path_for(self, name: str) -> Path:
        spec = self.spec(name)
        return self.model_dir / spec.filename if spec.filename else self.model_dir

    def tokenizer_path(self, name: str) -> Path:
        spec = self.spec(name)
        return self.model_dir / spec.tokenizer_filename

    def is_present(self, name: str) -> bool:
        spec = self.spec(name)
        if spec.kind == "spacy":
            try:
                import spacy

                return bool(spacy.util.is_package(spec.package))
            except Exception:
                return False
        return self.path_for(name).is_file() and self.tokenizer_path(name).is_file()

    def status(self) -> list[dict[str, Any]]:
        result = []
        for name, spec in BUILTIN_MODELS.items():
            item = spec.as_dict()
            present = self.is_present(name)
            active = False
            if present:
                try:
                    if spec.kind == "spacy":
                        active = self.spacy_pipeline() is not None
                    else:
                        self.onnx_session(name)
                        self.tokenizer(name)
                        active = True
                except Exception as exc:
                    active = False
                    log.debug("Model %s present but not loadable: %s", name, exc)
            item["present"] = present
            item["active"] = active
            item["path"] = str(self.path_for(name)) if spec.filename else spec.package
            item["badge"] = (
                "🟢 ONNX 已激活"
                if active and spec.kind.startswith("onnx")
                else ("🟢 已激活" if active else ("🟡 启发式降级中" if spec.kind.startswith("onnx") else "🟡 缺失"))
            )
            result.append(item)
        return result

    # -------------------------------------------------------------- downloads
    def ensure_model(
        self,
        name: str,
        *,
        download: bool = False,
        progress: ProgressCallback | None = None,
        force: bool = False,
        mirror_bases: Sequence[str] = MIRROR_BASES,
    ) -> bool:
        spec = self.spec(name)
        if spec.kind == "spacy":
            return self.ensure_spacy(download=download)
        if self.is_present(name) and not force:
            self._verify_sidecar(name)
            return True
        if not download:
            return False
        if spec.repo_id:
            self._download_from_hf(spec, progress=progress, mirror_bases=mirror_bases)
        elif spec.url:
            self._download_file(
                spec.url,
                self.path_for(name),
                progress=progress,
                label=name,
                min_bytes=spec.min_bytes,
            )
        else:
            return False
        self._verify_sidecar(name)
        self.reload(name)
        return self.is_present(name)

    def download_all(
        self,
        *,
        progress: ProgressCallback | None = None,
        force: bool = False,
        names: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Download and activate every built-in ONNX model.

        Each model falls back from ``hf-mirror.com`` to ``huggingface.co``.
        Returns a structured summary for the GUI / tests.
        """
        selected = [name for name in (names or BUILTIN_MODELS) if self.spec(name).kind != "spacy"]
        results: dict[str, Any] = {"ok": True, "models": {}, "errors": []}
        for index, name in enumerate(selected):
            try:
                self._emit(
                    progress,
                    {
                        "event": "model_start",
                        "model": name,
                        "index": index,
                        "total": len(selected),
                    },
                )
                ok = self.ensure_model(name, download=True, progress=progress, force=force)
                results["models"][name] = ok
                if not ok:
                    raise ModelUnavailable(f"{name} 下载后仍不可用")
            except Exception as exc:
                log.warning("Model download failed for %s: %s", name, exc)
                results["models"][name] = False
                results["errors"].append(f"{name}: {exc}")
                results["ok"] = False
        self._emit(progress, {"event": "all_done", "ok": results["ok"], "models": results["models"]})
        return results

    def import_from_folder(self, folder: str | Path) -> dict[str, Any]:
        """Copy model/tokenizer files from a user-selected local folder."""
        root = Path(folder)
        if not root.is_dir():
            raise ModelUnavailable(f"目录不存在：{root}")
        candidates = [path for path in root.rglob("*") if path.is_file()]
        result: dict[str, Any] = {"imported": {}, "missing": []}
        for name, spec in BUILTIN_MODELS.items():
            if spec.kind == "spacy":
                continue
            onnx = self._match_file(candidates, (spec.filename, "model_quantized.onnx", "model.onnx"))
            tokenizer = self._match_file(candidates, (spec.tokenizer_filename, "tokenizer.json"))
            if not onnx or not tokenizer:
                result["missing"].append(name)
                continue
            self.model_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(onnx, self.path_for(name))
            shutil.copy2(tokenizer, self.tokenizer_path(name))
            self.reload(name)
            result["imported"][name] = True
        if result["imported"]:
            self._emit(None, {"event": "imported", **result})
        return result

    @staticmethod
    def _match_file(candidates: Sequence[Path], names: Sequence[str]) -> Path | None:
        lowered = {str(name).lower(): name for name in names}
        exact = [path for path in candidates if path.name.lower() in lowered]
        if exact:
            # Prefer the most specific expected name.
            exact.sort(key=lambda path: list(lowered).index(path.name.lower()))
            return exact[0]
        return None

    def _download_from_hf(
        self,
        spec: ModelSpec,
        *,
        progress: ProgressCallback | None,
        mirror_bases: Sequence[str],
    ) -> None:
        last_error = ""
        for base in mirror_bases:
            try:
                onnx_url = self._resolve_url(base, spec.repo_id, spec.remote_filename)
                tokenizer_url = self._resolve_url(
                    base, spec.repo_id, spec.tokenizer_remote_filename
                )
                self._download_file(
                    onnx_url,
                    self.path_for(spec.name),
                    progress=progress,
                    label=f"{spec.name}:onnx",
                    min_bytes=spec.min_bytes,
                )
                self._download_file(
                    tokenizer_url,
                    self.tokenizer_path(spec.name),
                    progress=progress,
                    label=f"{spec.name}:tokenizer",
                    min_bytes=512,
                )
                self._validate_tokenizer(self.tokenizer_path(spec.name))
                self._validate_onnx(self.path_for(spec.name))
                log.info("Downloaded %s from %s", spec.name, base)
                return
            except Exception as exc:
                last_error = f"{base}: {exc}"
                log.warning("Model mirror failed for %s (%s): %s", spec.name, base, exc)
                self.path_for(spec.name).unlink(missing_ok=True)
                self.tokenizer_path(spec.name).unlink(missing_ok=True)
        raise ModelUnavailable(f"所有镜像均失败（{spec.name}）：{last_error}")

    @staticmethod
    def _resolve_url(base: str, repo_id: str, filename: str) -> str:
        return f"{base.rstrip('/')}/{repo_id}/resolve/main/{filename}"

    def _download_file(
        self,
        url: str,
        target: Path,
        *,
        progress: ProgressCallback | None = None,
        label: str = "",
        min_bytes: int = 1,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise ModelUnavailable("httpx 未安装，无法下载模型") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        total = 0
        downloaded = 0
        expected_sha = ""
        try:
            with httpx.Client(follow_redirects=True, timeout=120) as client:
                head = client.head(url)
                if head.status_code < 400:
                    total = int(head.headers.get("content-length") or 0)
                    etag = str(head.headers.get("etag") or "").strip('"')
                    if len(etag) == 64 and all(ch in "0123456789abcdef" for ch in etag.lower()):
                        expected_sha = etag.lower()
                self._emit(
                    progress,
                    {
                        "event": "start",
                        "label": label,
                        "url": url,
                        "total": total,
                    },
                )
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    if not total:
                        total = int(response.headers.get("content-length") or 0)
                    with open(tmp, "wb") as handle:
                        for chunk in response.iter_bytes(1024 * 256):
                            handle.write(chunk)
                            downloaded += len(chunk)
                            self._emit(
                                progress,
                                {
                                    "event": "progress",
                                    "label": label,
                                    "downloaded": downloaded,
                                    "total": total,
                                    "percent": (100.0 * downloaded / total) if total else 0.0,
                                },
                            )
            size = tmp.stat().st_size
            if size < min_bytes:
                raise ModelUnavailable(
                    f"文件不完整（{label}: {size} bytes < {min_bytes}）"
                )
            digest = self._sha256(tmp)
            if expected_sha and digest != expected_sha:
                # Some mirrors rewrite the served artifact while still
                # returning the upstream ETag.  Keep the computed SHA-256 as
                # the authoritative sidecar and let structural validation
                # (ONNX load / tokenizer JSON) decide whether it is usable.
                log.warning(
                    "Remote ETag differs from downloaded SHA-256 for %s; "
                    "falling back to structural validation",
                    label,
                )
            tmp.replace(target)
            target.with_suffix(target.suffix + ".sha256").write_text(
                digest + "\n", encoding="utf-8"
            )
            self._emit(
                progress,
                {
                    "event": "done",
                    "label": label,
                    "path": str(target),
                    "bytes": size,
                    "sha256": digest,
                },
            )
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest().lower()

    def _verify_sidecar(self, name: str) -> None:
        spec = self.spec(name)
        if spec.kind == "spacy" or not spec.filename:
            return
        path = self.path_for(name)
        if not path.is_file():
            return
        sidecar = path.with_suffix(path.suffix + ".sha256")
        expected = spec.sha256
        if not expected and sidecar.exists():
            expected = sidecar.read_text(encoding="utf-8").strip()
        if expected:
            self._verify_sha(path, expected)

    @classmethod
    def _verify_sha(cls, path: Path, expected: str) -> None:
        if cls._sha256(path) != str(expected).lower():
            raise ModelUnavailable(f"模型校验失败: {path}")

    @staticmethod
    def _validate_tokenizer(path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ModelUnavailable(f"Tokenizer 不是有效 JSON: {path}") from exc
        if not isinstance(data, dict) or not data.get("model"):
            raise ModelUnavailable(f"Tokenizer 内容异常: {path}")

    @staticmethod
    def _validate_onnx(path: Path) -> None:
        try:
            import onnxruntime as ort

            session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
            if not session.get_inputs() or not session.get_outputs():
                raise ValueError("ONNX 模型没有输入/输出")
        except Exception as exc:
            raise ModelUnavailable(f"ONNX 模型结构校验失败 {path}: {exc}") from exc

    @staticmethod
    def _emit(progress: ProgressCallback | None, event: dict[str, Any]) -> None:
        if progress is None:
            return
        try:
            progress(event)
        except Exception:
            log.debug("Progress callback failed", exc_info=True)

    def reload(self, name: str | None = None) -> None:
        names = [name] if name else list(BUILTIN_MODELS)
        for item in names:
            self._onnx_cache.pop(item, None)
            self._tokenizer_cache.pop(item, None)

    # ---------------------------------------------------------------- spaCy
    def ensure_spacy(self, *, download: bool = False) -> bool:
        try:
            import spacy

            if spacy.util.is_package("en_core_web_sm"):
                return True
            if download:
                spacy.cli.download("en_core_web_sm")
                return spacy.util.is_package("en_core_web_sm")
        except Exception as exc:
            log.info("spaCy check failed: %s", exc)
        return False

    def spacy_pipeline(self):
        if self._spacy_tried:
            return self._spacy
        self._spacy_tried = True
        try:
            import spacy

            self._spacy = spacy.load("en_core_web_sm")
            self._spacy.max_length = 2_000_000
            log.info("Loaded spaCy en_core_web_sm")
        except Exception as exc:
            log.info("spaCy en_core_web_sm unavailable: %s", exc)
            self._spacy = None
        return self._spacy

    # ---------------------------------------------------------------- ONNX
    def onnx_session(self, name: str):
        if name in self._onnx_cache:
            return self._onnx_cache[name]
        spec = self.spec(name)
        path = self.path_for(name)
        if not path.is_file():
            raise ModelUnavailable(f"缺少 ONNX 模型: {path}")
        try:
            import onnxruntime as ort

            options = ort.SessionOptions()
            options.enable_cpu_mem_arena = False
            options.enable_mem_pattern = False
            options.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
            session = ort.InferenceSession(
                str(path),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
        except Exception as exc:
            raise ModelUnavailable(f"ONNX 模型加载失败 {path}: {exc}") from exc
        self._onnx_cache[name] = session
        return session

    def tokenizer(self, name: str):
        if name in self._tokenizer_cache:
            return self._tokenizer_cache[name]
        path = self.tokenizer_path(name)
        if not path.is_file():
            raise ModelUnavailable(f"缺少 tokenizer: {path}")
        try:
            from tokenizers import Tokenizer

            tokenizer = Tokenizer.from_file(str(path))
            tokenizer.enable_truncation(max_length=512)
            tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        except Exception as exc:
            raise ModelUnavailable(f"Tokenizer 加载失败 {path}: {exc}") from exc
        self._tokenizer_cache[name] = tokenizer
        return tokenizer

    @staticmethod
    def _feed(session, input_ids, attention_mask):
        inputs: dict[str, Any] = {}
        names = {item.name for item in session.get_inputs()}
        first = session.get_inputs()[0].name
        inputs[first] = input_ids
        for candidate in ("attention_mask", "input_mask"):
            if candidate in names:
                inputs[candidate] = attention_mask
                break
        if "token_type_ids" in names:
            try:
                import numpy as np

                inputs["token_type_ids"] = np.zeros_like(input_ids)
            except Exception:
                pass
        return session.run(None, inputs)

    @staticmethod
    def _softmax(values):
        import numpy as np

        array = np.asarray(values, dtype=np.float32)
        array = array - np.max(array, axis=-1, keepdims=True)
        exp = np.exp(array)
        return exp / np.sum(exp, axis=-1, keepdims=True)

    def embed_minilm(
        self,
        texts: Sequence[str],
        *,
        batch_size: int = 32,
        progress: Callable[[int, int], None] | None = None,
    ) -> list[list[float]]:
        """Sentence embeddings from the 384-d MiniLM ONNX model.

        Batching is mandatory: encoding a whole 3k-card library at once can
        allocate several GB for the token_embeddings tensor.
        """
        import numpy as np

        if self.spec("mini_cluster_embed").dimension != 384:
            raise ModelUnavailable("MiniLM 维度配置异常")
        values = [str(text or "") for text in texts]
        if not values:
            return []
        batch_size = max(1, min(256, int(batch_size or 32)))
        tokenizer = self.tokenizer("mini_cluster_embed")
        session = self.onnx_session("mini_cluster_embed")
        vectors: list[list[float]] = []
        for start in range(0, len(values), batch_size):
            batch = values[start:start + batch_size]
            encodings = tokenizer.encode_batch(batch)
            input_ids = np.array([enc.ids for enc in encodings], dtype=np.int64)
            attention = np.array(
                [[1 if token != 0 else 0 for token in enc.ids] for enc in encodings],
                dtype=np.int64,
            )
            outputs = self._feed(session, input_ids, attention)
            hidden = np.asarray(outputs[0], dtype=np.float32)
            if hidden.ndim == 3:
                mask = attention[..., None].astype(np.float32)
                pooled = np.sum(hidden * mask, axis=1) / np.clip(
                    np.sum(mask, axis=1), 1e-9, None
                )
            else:
                pooled = hidden
            norm = np.linalg.norm(pooled, axis=1, keepdims=True)
            pooled = pooled / np.clip(norm, 1e-9, None)
            vectors.extend(pooled.tolist())
            if progress is not None:
                try:
                    progress(min(len(values), start + len(batch)), len(values))
                except Exception:
                    log.debug("MiniLM progress callback failed", exc_info=True)
        return vectors

    def rerank_local(self, query: str, documents: Sequence[str]) -> list[float]:
        import numpy as np

        tokenizer = self.tokenizer("local_reranker")
        session = self.onnx_session("local_reranker")
        encodings = tokenizer.encode_batch(
            [(query, document) for document in documents]
        )
        input_ids = np.array([enc.ids for enc in encodings], dtype=np.int64)
        attention = np.array(
            [[1 if token != 0 else 0 for token in enc.ids] for enc in encodings],
            dtype=np.int64,
        )
        outputs = self._feed(session, input_ids, attention)
        logits = np.asarray(outputs[0], dtype=np.float32)
        if logits.ndim == 2 and logits.shape[1] > 1:
            logits = logits[:, 1]
        logits = logits.reshape(-1)
        probs = 1.0 / (1.0 + np.exp(-logits))
        return probs.tolist()

    def nli_local(self, premise: str, hypothesis: str) -> dict[str, float]:
        import numpy as np

        tokenizer = self.tokenizer("conflict_nli")
        session = self.onnx_session("conflict_nli")
        encoding = tokenizer.encode(premise, hypothesis)
        input_ids = np.array([encoding.ids], dtype=np.int64)
        attention = np.array(
            [[1 if token != 0 else 0 for token in encoding.ids]], dtype=np.int64
        )
        outputs = self._feed(session, input_ids, attention)
        logits = np.asarray(outputs[0], dtype=np.float32).reshape(-1)
        probs = self._softmax(logits).reshape(-1)
        labels = self.spec("conflict_nli").labels
        if len(labels) != len(probs):
            labels = [f"label_{index}" for index in range(len(probs))]
        return {label: float(score) for label, score in zip(labels, probs)}

    def nli_batch(
        self,
        pairs: list[tuple[str, str]],
        *,
        batch_size: int = 16,
    ) -> list[dict[str, float]]:
        """Run DeBERTa NLI on sequence pairs with bounded memory use."""
        import numpy as np

        if not pairs:
            return []
        tokenizer = self.tokenizer("conflict_nli")
        session = self.onnx_session("conflict_nli")
        labels = self.spec("conflict_nli").labels
        batch_size = max(1, min(64, int(batch_size or 16)))
        results: list[dict[str, float]] = []
        for start in range(0, len(pairs), batch_size):
            batch = pairs[start:start + batch_size]
            encodings = tokenizer.encode_batch(batch)
            max_length = max((len(encoding.ids) for encoding in encodings), default=1)
            input_ids = np.zeros((len(encodings), max_length), dtype=np.int64)
            attention = np.zeros((len(encodings), max_length), dtype=np.int64)
            for row, encoding in enumerate(encodings):
                ids = list(encoding.ids)
                input_ids[row, :len(ids)] = ids
                mask = list(getattr(encoding, "attention_mask", []) or [])
                if len(mask) != len(ids):
                    mask = [1 if token != 0 else 0 for token in ids]
                attention[row, :len(mask)] = mask
            outputs = self._feed(session, input_ids, attention)
            logits = np.asarray(outputs[0], dtype=np.float32)
            if logits.ndim == 1:
                logits = logits.reshape(1, -1)
            elif logits.ndim == 3:
                logits = logits.reshape(logits.shape[0], -1)
            probabilities = self._softmax(logits)
            for row in probabilities:
                row_labels = labels
                if len(row_labels) != len(row):
                    row_labels = [f"label_{index}" for index in range(len(row))]
                results.append(
                    {
                        label: float(score)
                        for label, score in zip(row_labels, row)
                    }
                )
        return results
