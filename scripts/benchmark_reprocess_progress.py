"""Read-only benchmark of real-library recluster progress and ETA."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app_core"))

from builtin_models.model_registry import ModelRegistry
from db.sqlite_manager import SQLiteManager
from parser.consensus_conflict_engine import ConsensusConflictEngine
from reprocess import ReprocessManager
from ui.progress_eta import EtaTracker

SOURCE = Path(r"C:\Normal Program Files\AtomicCalculationDB\workspace\atomic_meta.sqlite")
MODELS = ROOT / "workspace"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="acdb-progress-") as temp:
        work = Path(temp)
        db_path = work / "atomic_meta.sqlite"
        source = sqlite3.connect(f"file:{SOURCE.as_posix()}?mode=ro", uri=True)
        target = sqlite3.connect(db_path)
        source.backup(target)
        source.close()
        target.close()

        db = SQLiteManager(work, embedding_signature="test::dim=1024")
        registry = ModelRegistry(MODELS)
        engine = ConsensusConflictEngine(registry=registry)
        pipeline = SimpleNamespace(
            consensus=engine,
            gateway=SimpleNamespace(
                embedding=SimpleNamespace(release_local_session=lambda: None)
            ),
            qdrant=None,
        )
        manager = ReprocessManager(db, work, pipeline)
        tracker = EtaTracker()
        tracker.reset()
        started = time.monotonic()
        last_log = [0.0, -1.0, ""]

        def progress(label: str, fraction: float) -> None:
            now = time.monotonic()
            summary = tracker.summary(fraction, label)
            changed = (
                now - last_log[0] >= 5.0
                or fraction - last_log[1] >= 0.05
                or label != last_log[2]
            )
            if changed:
                last_log[:] = [now, fraction, label]
                print(
                    f"[{now - started:7.1f}s] {fraction * 100:5.1f}% "
                    f"{label} | {summary}",
                    flush=True,
                )

        print(f"cards={db.count_knowledge_cards()} source={SOURCE}", flush=True)
        result = manager._recluster_local(progress=progress)
        elapsed = time.monotonic() - started
        print(f"RESULT {result} elapsed={elapsed:.1f}s", flush=True)
        db.close()


if __name__ == "__main__":
    main()
