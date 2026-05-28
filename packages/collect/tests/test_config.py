"""Hub config + config directory."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from fulcra_collect import config


def test_config_dir_honours_the_env_override(collect_home: Path):
    assert config.config_dir() == collect_home


def test_config_dir_is_owner_only(collect_home: Path):
    """M2: the config dir holds the control socket and state — 0700 only."""
    d = config.config_dir()
    mode = stat.S_IMODE(os.stat(d).st_mode)
    assert mode == 0o700, f"expected 0700, got {oct(mode)}"


def test_load_returns_empty_config_when_no_file(collect_home: Path):
    cfg = config.load()
    assert cfg.enabled == set()
    assert cfg.interval_overrides == {}
    assert cfg.plugin_settings == {}


def test_enable_disable_round_trip(collect_home: Path):
    cfg = config.load()
    cfg.enable("lastfm")
    cfg.enable("dayone")
    cfg.disable("dayone")
    config.save(cfg)
    reloaded = config.load()
    assert reloaded.enabled == {"lastfm"}


def test_interval_override_round_trip(collect_home: Path):
    cfg = config.load()
    cfg.set_interval("lastfm", 1800)
    config.save(cfg)
    assert config.load().interval_overrides == {"lastfm": 1800}


def test_plugin_settings_round_trip(collect_home: Path):
    cfg = config.load()
    cfg.plugin_settings["dayone"] = {"local_db": True}
    config.save(cfg)
    assert config.load().plugin_settings["dayone"] == {"local_db": True}


def test_save_preserves_comments(collect_home: Path):
    """config.save() must not strip comments the user added by hand."""
    toml_path = config.config_dir() / "config.toml"
    toml_path.write_text(
        "# this is a comment\nenabled = [\"lastfm\"]\n",
        encoding="utf-8",
    )
    cfg = config.load()
    assert cfg.enabled == {"lastfm"}
    cfg.enable("dayone")
    config.save(cfg)
    raw = toml_path.read_text(encoding="utf-8")
    assert "# this is a comment" in raw, (
        "tomlkit round-trip should preserve the hand-written comment"
    )


# ---------------------------------------------------------------------------
# Daemon-wide settings: web_port
# ---------------------------------------------------------------------------

def test_default_web_port_is_9292(collect_home: Path):
    """Empty config → web_port defaults to 9292 (the stable daemon port)."""
    cfg = config.load()
    assert cfg.web_port == 9292


def test_web_port_override_via_daemon_table(collect_home: Path):
    """A user-set `[daemon] web_port = N` in config.toml is honored on load."""
    toml_path = config.config_dir() / "config.toml"
    toml_path.write_text(
        "[daemon]\nweb_port = 9595\n",
        encoding="utf-8",
    )
    cfg = config.load()
    assert cfg.web_port == 9595


def test_web_port_round_trip(collect_home: Path):
    """save → load preserves a non-default web_port."""
    cfg = config.load()
    cfg.web_port = 9999
    config.save(cfg)
    assert config.load().web_port == 9999


def test_default_web_port_not_persisted(collect_home: Path):
    """Saving the default port should not write a `[daemon]` table — keeps
    the default config minimal and means the value is read from the
    code's default constant."""
    cfg = config.load()
    config.save(cfg)
    toml_path = config.config_dir() / "config.toml"
    raw = toml_path.read_text(encoding="utf-8")
    assert "[daemon]" not in raw and "web_port" not in raw
