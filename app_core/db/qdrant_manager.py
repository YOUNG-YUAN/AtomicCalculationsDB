"""Qdrant local-mode wrapper for the 1024-d BGE-M3 vector space.

The collection stores one HNSW point per L2 knowledge card.  L1 master cards
are intentionally *not* vectorised; they are pinned by search metadata and
authority instead.  Payload indexes mirror the four physics coordinates so the
hybrid retriever can do a pre-filter before ANN search.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

log = logging.getLogger(__name__)


class QdrantUnavailable(RuntimeError):
    pass


def point_id(knw_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"atomic-calculation-db:v2:{knw_id}"))


@dataclass
class QdrantStatus:
    enabled: bool
    ok: bool
    collection: str = ""
    dimension: int = 0
    count: int = 0
    path: str = ""
    message: str = ""
    signature_mismatch: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class QdrantManager:
    """Thin, version-tolerant wrapper around ``QdrantClient(path=...)``."""

    def __init__(
        self,
        workspace_dir: str | Path,
        *,
        collection: str = "knowledge_cards",
        dimension: int = 1024,
        embedding_signature: str | None = None,
        enabled: bool = True,
    ):
        self.workspace = Path(workspace_dir)
        self.path = self.workspace / "vector_db"
        self.collection = collection
        self.dimension = int(dimension)
        self.enabled = enabled
        self.signature = embedding_signature or ""
        self.signature_mismatch = False
        self.client: Any = None
        self._models: Any = None
        self.load_error = ""
        self.meta_path = self.path / "collection_meta.json"
        if not enabled:
            return
        self.path.mkdir(parents=True, exist_ok=True)
        try:
            from qdrant_client import QdrantClient, models

            self._models = models
            self.client = QdrantClient(path=str(self.path))
            self._ensure_collection()
        except Exception as exc:  # pragma: no cover - environment dependent
            self.load_error = str(exc)
            log.warning("Qdrant local store unavailable: %s", exc)
            if self.client is not None:
                try:
                    self.client.close()
                except Exception:
                    pass
            self.client = None

    # ------------------------------------------------------------- lifecycle
    def _ensure_collection(self) -> None:
        models = self._models
        if not self.client.collection_exists(self.collection):
            try:
                self.client.create_collection(
                    collection_name=self.collection,
                    vectors_config=models.VectorParams(
                        size=self.dimension,
                        distance=models.Distance.COSINE,
                        hnsw_config=models.HnswConfigDiff(m=16, ef_construct=100),
                    ),
                )
            except Exception:
                # Older qdrant-client / local backend fallback.
                self.client.create_collection(
                    collection_name=self.collection,
                    vectors_config=models.VectorParams(
                        size=self.dimension, distance=models.Distance.COSINE
                    ),
                )
            log.info(
                "Created Qdrant collection %s (dim=%d)", self.collection, self.dimension
            )
            self._create_payload_indexes()
            self._write_meta()
            return

        info = self.client.get_collection(self.collection)
        existing_dim = self._collection_dimension(info)
        if existing_dim and existing_dim != self.dimension:
            raise QdrantUnavailable(
                f"向量维度不匹配：collection={existing_dim}, config={self.dimension}"
            )
        self._read_meta()
        self._create_payload_indexes()

    def _create_payload_indexes(self) -> None:
        if not self.client:
            return
        models = self._models
        fields = {
            "card_level": models.PayloadSchemaType.KEYWORD,
            "card_type": models.PayloadSchemaType.KEYWORD,
            "element": models.PayloadSchemaType.KEYWORD,
            "isoelectronic": models.PayloadSchemaType.KEYWORD,
            "method": models.PayloadSchemaType.KEYWORD,
            "code_name": models.PayloadSchemaType.KEYWORD,
            "physical_process": models.PayloadSchemaType.TEXT,
            "z_num": models.PayloadSchemaType.INTEGER,
            "has_conflict": models.PayloadSchemaType.BOOL,
            "conclusion_flags": models.PayloadSchemaType.INTEGER,
            "doc_id": models.PayloadSchemaType.KEYWORD,
        }
        import warnings

        for field, schema in fields.items():
            try:
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore", message="Payload indexes have no effect.*"
                    )
                    self.client.create_payload_index(
                        collection_name=self.collection,
                        field_name=field,
                        field_schema=schema,
                    )
            except Exception as exc:  # local mode may no-op
                log.debug("Payload index %s skipped: %s", field, exc)

    def _write_meta(self) -> None:
        data = {
            "collection": self.collection,
            "dimension": self.dimension,
            "embedding_signature": self.signature,
        }
        try:
            self.meta_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            log.warning("Could not write Qdrant metadata")

    def _read_meta(self) -> None:
        if not self.meta_path.exists():
            self._write_meta()
            return
        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        stored = str(data.get("embedding_signature") or "")
        if self.signature and stored and stored != self.signature:
            self.signature_mismatch = True
            log.error(
                "Qdrant embedding signature mismatch: stored=%s configured=%s",
                stored,
                self.signature,
            )
        if not stored and self.signature:
            self._write_meta()

    @staticmethod
    def _collection_dimension(info: Any) -> int | None:
        try:
            vectors = info.config.params.vectors
            if hasattr(vectors, "size"):
                return int(vectors.size)
            if isinstance(vectors, dict) and vectors:
                return int(next(iter(vectors.values())).size)
        except Exception:
            return None
        return None

    @property
    def available(self) -> bool:
        return self.client is not None and not self.signature_mismatch

    def close(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None

    # ------------------------------------------------------------------ CRUD
    def upsert_card(
        self,
        knw_id: str,
        vector: Sequence[float],
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        self.upsert_many([(knw_id, vector, dict(payload or {}))])

    def upsert_many(
        self, items: Iterable[tuple[str, Sequence[float], Mapping[str, Any]]]
    ) -> int:
        if not self.client:
            raise QdrantUnavailable(self.load_error or "Qdrant 未启用")
        points = []
        for knw_id, vector, payload in items:
            if len(vector) != self.dimension:
                raise QdrantUnavailable(
                    f"向量维度 {len(vector)} 与集合维度 {self.dimension} 不一致"
                )
            body = dict(payload)
            body["entity_id"] = knw_id
            body.setdefault("knw_id", knw_id)
            points.append(
                self._models.PointStruct(
                    id=point_id(knw_id), vector=list(vector), payload=body
                )
            )
        if points:
            self.client.upsert(
                collection_name=self.collection, points=points, wait=True
            )
        return len(points)

    def delete(self, knw_id: str) -> None:
        if not self.client:
            return
        self.client.delete(
            collection_name=self.collection,
            points_selector=self._models.PointIdsList(points=[point_id(knw_id)]),
            wait=True,
        )

    def search(
        self,
        vector: Sequence[float],
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        if not self.available:
            return []
        if len(vector) != self.dimension:
            raise QdrantUnavailable(
                f"查询向量维度 {len(vector)} 与集合维度 {self.dimension} 不一致"
            )
        query_filter = self.build_filter(filters or {})
        if hasattr(self.client, "query_points"):
            response = self.client.query_points(
                collection_name=self.collection,
                query=list(vector),
                query_filter=query_filter,
                limit=max(1, int(limit)),
                with_payload=True,
            )
            points = response.points
        else:  # pragma: no cover - older client
            points = self.client.search(
                collection_name=self.collection,
                query_vector=list(vector),
                query_filter=query_filter,
                limit=max(1, int(limit)),
                with_payload=True,
            )
        return [
            {
                "knw_id": (point.payload or {}).get("knw_id")
                or (point.payload or {}).get("entity_id"),
                "score": float(point.score),
                "payload": dict(point.payload or {}),
            }
            for point in points
        ]

    def build_filter(self, filters: Mapping[str, Any]) -> Any:
        if not self._models:
            return None
        models = self._models
        must: list[Any] = []
        exact = ("card_level", "card_type", "element", "isoelectronic", "code_name")
        for field in exact:
            value = filters.get(field)
            if value not in (None, ""):
                must.append(
                    models.FieldCondition(
                        key=field, match=models.MatchValue(value=str(value))
                    )
                )
        if filters.get("z_num") not in (None, ""):
            must.append(
                models.FieldCondition(
                    key="z_num", match=models.MatchValue(value=int(filters["z_num"]))
                )
            )
        # method / physical_process are intentionally post-filtered in the
        # retriever because cards store composite labels such as MCDHF+RCI and
        # Qdrant payload indexes are exact-match oriented.
        if filters.get("has_conflict") not in (None, ""):
            must.append(
                models.FieldCondition(
                    key="has_conflict",
                    match=models.MatchValue(value=1 if filters["has_conflict"] else 0),
                )
            )
        if filters.get("conclusion_flags") not in (None, "", 0):
            # Qdrant lacks bitwise filters; retriever post-filters these hits.
            pass
        if not must:
            return None
        return models.Filter(must=must)

    def export_all(self, limit: int = 10000) -> list[dict[str, Any]]:
        """Scroll the whole collection with vectors for ``.acdb`` export."""
        if not self.client:
            return []
        points: list[Any] = []
        offset = None
        while True:
            page, offset = self.client.scroll(
                collection_name=self.collection,
                limit=min(1000, max(1, int(limit))),
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            points.extend(page)
            if offset is None or len(points) >= limit:
                break
        result: list[dict[str, Any]] = []
        for point in points[:limit]:
            payload = dict(point.payload or {})
            vector = getattr(point, "vector", None)
            if isinstance(vector, dict):
                vector = next(iter(vector.values()), None)
            result.append(
                {
                    "knw_id": payload.get("knw_id") or payload.get("entity_id"),
                    "vector": list(vector or []),
                    "payload": payload,
                }
            )
        return result

    def count(self) -> int:
        if not self.client:
            return 0
        try:
            return int(self.client.count(self.collection, exact=True).count)
        except Exception:
            return 0

    def status(self) -> QdrantStatus:
        if not self.enabled:
            return QdrantStatus(enabled=False, ok=False, message="已禁用")
        if not self.client:
            return QdrantStatus(
                enabled=True,
                ok=False,
                collection=self.collection,
                dimension=self.dimension,
                path=str(self.path),
                message=self.load_error or "无法打开向量库",
            )
        return QdrantStatus(
            enabled=True,
            ok=not self.signature_mismatch and not bool(self.load_error),
            collection=self.collection,
            dimension=self.dimension,
            count=self.count(),
            path=str(self.path),
            message=(
                "Embedding 签名不一致，需重建向量库"
                if self.signature_mismatch
                else self.load_error
            ),
            signature_mismatch=self.signature_mismatch,
        )

    def rebuild_collection(self) -> None:
        if not self.client:
            raise QdrantUnavailable("Qdrant 未启用")
        try:
            self.client.delete_collection(self.collection)
        except Exception:
            pass
        self._ensure_collection()
