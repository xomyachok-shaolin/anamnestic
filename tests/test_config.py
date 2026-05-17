import importlib
import os


_DATA_ENV = ("ANAMNESTIC_DATA_DIR", "CLAUDE_MEM_DATA_DIR")
_ORIGINAL = {name: os.environ.get(name) for name in _DATA_ENV}


def _reload_config(monkeypatch, **env):
    for name in _DATA_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    import anamnestic.config as config

    return importlib.reload(config)


def _restore_env():
    for name, value in _ORIGINAL.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def test_claude_mem_data_dir_is_default_profile(monkeypatch):
    config = _reload_config(monkeypatch, CLAUDE_MEM_DATA_DIR="/tmp/claude-mem-work")
    try:
        assert str(config.DATA_DIR) == "/tmp/claude-mem-work"
        assert config.DB_PATH == "/tmp/claude-mem-work/claude-mem.db"
    finally:
        _restore_env()
        importlib.reload(config)


def test_anamnestic_data_dir_overrides_claude_mem_profile(monkeypatch):
    config = _reload_config(
        monkeypatch,
        CLAUDE_MEM_DATA_DIR="/tmp/claude-mem-work",
        ANAMNESTIC_DATA_DIR="/tmp/anamnestic",
    )
    try:
        assert str(config.DATA_DIR) == "/tmp/anamnestic"
        assert config.DB_PATH == "/tmp/anamnestic/claude-mem.db"
    finally:
        _restore_env()
        importlib.reload(config)
