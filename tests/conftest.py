import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
APP_CORE = ROOT / "app_core"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(APP_CORE) not in sys.path:
    sys.path.insert(0, str(APP_CORE))

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
