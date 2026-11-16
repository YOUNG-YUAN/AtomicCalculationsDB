import json
import zipfile
from pathlib import Path

import pytest

from updater.updater import (
    UpdateError,
    apply_pending_update,
    inspect_package,
    pending_path,
    read_version,
    stage_package,
)


def _make_zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def _release(base: Path) -> Path:
    core = base / "app_core"
    core.mkdir(parents=True)
    (core / "version.json").write_text(
        json.dumps({"version": "0.1.0", "build": "old"}), encoding="utf-8"
    )
    (core / "main_entry.py").write_text("OLD = True\n", encoding="utf-8")
    (base / "workspace").mkdir()
    return core


def test_inspect_and_apply_update(tmp_path: Path):
    base = tmp_path / "release"
    core = _release(base)
    package = _make_zip(
        tmp_path / "update.zip",
        {
            "version.json": json.dumps({"version": "9.9.9", "build": "new"}),
            "main_entry.py": "NEW = True\n",
            "ui/__init__.py": "",
            "ui/app.py": "print('hello')\n",
        },
    )

    info = inspect_package(package)
    assert info.version == "9.9.9"
    assert info.entries == 4

    staged = stage_package(base, package)
    assert staged == pending_path(base)
    assert staged.exists()

    applied = apply_pending_update(base)
    assert applied is not None
    assert applied.version == "9.9.9"
    assert (core / "main_entry.py").read_text(encoding="utf-8") == "NEW = True\n"
    assert (core / "ui" / "app.py").exists()
    assert not staged.exists()
    backup = base / "app_core_bak"
    assert backup.exists()
    assert (backup / "main_entry.py").read_text(encoding="utf-8") == "OLD = True\n"
    assert read_version(core)["version"] == "9.9.9"


def test_reject_path_traversal(tmp_path: Path):
    package = _make_zip(
        tmp_path / "evil.zip",
        {
            "version.json": json.dumps({"version": "1.0.1"}),
            "main_entry.py": "x = 1\n",
            "../evil.txt": "oops",
        },
    )
    with pytest.raises(UpdateError):
        inspect_package(package)


def test_reject_invalid_version(tmp_path: Path):
    package = _make_zip(
        tmp_path / "bad.zip",
        {"version.json": "not json", "main_entry.py": "x = 1\n"},
    )
    with pytest.raises(UpdateError):
        inspect_package(package)


def test_failed_update_keeps_core(tmp_path: Path):
    base = tmp_path / "release"
    core = _release(base)
    package = _make_zip(
        tmp_path / "incomplete.zip",
        {"version.json": json.dumps({"version": "1.0.2"})},
    )
    stage_package(base, package)
    with pytest.raises(UpdateError):
        apply_pending_update(base)
    assert (core / "main_entry.py").read_text(encoding="utf-8") == "OLD = True\n"
    assert pending_path(base).exists()


def test_apply_returns_none_without_package(tmp_path: Path):
    base = tmp_path / "release"
    _release(base)
    assert apply_pending_update(base) is None


def test_clean_launch_env_strips_pyinstaller_runtime(monkeypatch):
    from updater.updater import clean_launch_env

    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", r"C:\Temp\_MEI123")
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")
    monkeypatch.setenv("_MEIPASS2", r"C:\Temp\_MEI123")
    monkeypatch.setenv("ACDB_AUTO_HOT_RESTART", "patch.zip")
    monkeypatch.setenv("KEEP_ME", "ok")
    env = clean_launch_env()
    assert "_PYI_APPLICATION_HOME_DIR" not in env
    assert "_PYI_PARENT_PROCESS_LEVEL" not in env
    assert "_MEIPASS2" not in env
    assert "ACDB_AUTO_HOT_RESTART" not in env
    assert env["KEEP_ME"] == "ok"
