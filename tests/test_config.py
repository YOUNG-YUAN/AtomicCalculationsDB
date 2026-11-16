from pathlib import Path

import yaml

from config import AppConfig, DEFAULTS


def test_defaults_created(tmp_path: Path):
    path = tmp_path / "workspace" / "config.yaml"
    config = AppConfig.load(path)
    assert path.exists()
    assert config.get("night_shift.start") == "18:00"
    assert config.get("endpoint_pool.nodes") == []
    assert config.get("embedding.dim") is None
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["app"]["name"] == "AtomicCalculationDB"


def test_partial_config_merges_defaults(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({"night_shift": {"start": "21:30"}}), encoding="utf-8"
    )
    config = AppConfig.load(path)
    assert config.get("night_shift.start") == "21:30"
    assert config.get("night_shift.end") == DEFAULTS["night_shift"]["end"]
    assert config.get("endpoint_pool.role_bindings") == {}
    assert config.get("models.fast_llm.provider") is None


def test_set_save_and_reload(tmp_path: Path):
    path = tmp_path / "config.yaml"
    config = AppConfig.load(path)
    config.set("app.language", "en-US")
    config.set("crawler.fallback_max_year", 2020)
    config.save()

    reloaded = AppConfig.load(path)
    assert reloaded.get("app.language") == "en-US"
    assert reloaded.get("crawler.fallback_max_year") == 2020


def test_env_secret(monkeypatch, tmp_path: Path):
    config = AppConfig.load(tmp_path / "config.yaml")
    config.set("sync.s3.secret_key", "env:ACDB_TEST_KEY")
    monkeypatch.setenv("ACDB_TEST_KEY", "secret-value")
    assert config.resolve_secret("sync.s3.secret_key") == "secret-value"
    config.set("sync.s3.secret_key", "plain-key")
    assert config.resolve_secret("sync.s3.secret_key") == "plain-key"
