# Built-in lightweight models (CPU only, no PyTorch)

All runtime inference uses **ONNX Runtime CPU** or **spaCy**. The application
does not import `torch`. Model binaries are user-supplied or downloaded into
`workspace/models/` (never into `app_core/`, so hot updates stay small).

| Registry name | Kind | Expected file(s) | Dimension / labels | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| `spacy_en` | spaCy package | `en_core_web_sm` (installed package) | — | Sentence segmentation, dependency clauses (advcl/relcl), condition protection |
| `mini_cluster_embed` | ONNX embedding | `all-MiniLM-L6-v2-int8.onnx` + `all-MiniLM-L6-v2-tokenizer.json` | 384 | In-memory synonym clustering of conclusions |
| `conflict_nli` | ONNX NLI | `nli-deberta-v3-xsmall-int8.onnx` + `nli-deberta-v3-xsmall-tokenizer.json` | `contradiction`, `neutral`, `entailment` | Polarity conflict radar |
| `local_reranker` | ONNX cross-encoder | `ms-marco-MiniLM-L-6-v2-int8.onnx` + `ms-marco-MiniLM-L-6-v2-tokenizer.json` | score | Local reranking fallback |

The **global retrieval embedding** is a separate 1024-d `BAAI/bge-m3` model,
configured in `models/` gateway (cloud API, Ollama, or local ONNX). Its
signature is locked in `system_meta.embedding_signature` and in
`vector_db/collection_meta.json`.

## Installation

```bash
python -m spacy download en_core_web_sm
```

For ONNX models, open **Settings → 本地轻量神经网络引擎 (ONNX)** and click
**「⬇️ 一键下载并激活全部内置 ONNX 模型」**.  The downloader tries
`hf-mirror.com` first and falls back to `huggingface.co`; it streams progress,
validates ONNX structure/tokenizer JSON, and stores a computed SHA-256 sidecar
for subsequent integrity checks.  You can also import an existing folder with
the button **「📂 从本地文件夹导入模型」**.

Expected local filenames after download/import:

```text
workspace/models/
├── all-MiniLM-L6-v2-int8.onnx(.sha256)
├── all-MiniLM-L6-v2-tokenizer.json(.sha256)
├── nli-deberta-v3-xsmall-int8.onnx(.sha256)
├── nli-deberta-v3-xsmall-tokenizer.json(.sha256)
├── ms-marco-MiniLM-L-6-v2-int8.onnx(.sha256)
└── ms-marco-MiniLM-L-6-v2-tokenizer.json(.sha256)
```

## Graceful degradation

* Missing spaCy → regex sentence splitting and no dependency clauses.
* Missing MiniLM → token-set cosine clustering.
* Missing NLI → heuristic antonym/negation conflict detection.
* Missing reranker → RRF + BM25 + authority/recency fusion.
* Missing BGE-M3 → FTS-only retrieval; Qdrant is not written.

This means the application is fully usable before the optional ONNX models are
installed; model availability is shown as 🟢 `ONNX 已激活` / 🟡 `启发式降级中` in **Settings → 本地轻量神经网络引擎**.
