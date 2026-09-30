"""Boolean config fields must coerce from the CLI and from hand-edited JSON alike.

`zotpilot config set oa_pdf_upload false` used to store the string "false", and
Config.load's bool("false") read it back as True — enabling the very upload the
setting exists to disable.
"""
from __future__ import annotations

import json

import pytest

from zotpilot.cli import _coerce_value
from zotpilot.config import Config


@pytest.mark.parametrize("key", ["oa_pdf_upload", "deploy_skills"])
def test_cli_coerces_false_to_bool(key):
    assert _coerce_value(key, "false") is False
    assert _coerce_value(key, "true") is True


def test_deploy_skills_defaults_true(tmp_path):
    assert Config.load(tmp_path / "missing.json").deploy_skills is True


def test_deploy_skills_false_round_trips(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"deploy_skills": False}))
    cfg = Config.load(path)
    assert cfg.deploy_skills is False
    cfg.save(path)
    assert json.loads(path.read_text())["deploy_skills"] is False


# key -> default used by Config.load when the key is absent
BOOL_DEFAULTS = {
    "oa_pdf_upload": False,
    "deploy_skills": True,
    "preflight_enabled": True,
    "rerank_enabled": True,
    "vision_enabled": True,
}


def _load(tmp_path, key, value):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({key: value}))
    return getattr(Config.load(path), key)


@pytest.mark.parametrize("key", list(BOOL_DEFAULTS))
def test_string_false_is_false(tmp_path, key):
    assert _load(tmp_path, key, "false") is False


@pytest.mark.parametrize("key", list(BOOL_DEFAULTS))
@pytest.mark.parametrize(
    "raw, expected",
    [("off", False), ("on", True), ("0", False), ("no", False), (" False ", False),
     ("y", True), ("n", False), ("1", True), (True, True), (False, False)],
)
def test_string_and_json_bools(tmp_path, key, raw, expected):
    assert _load(tmp_path, key, raw) is expected


@pytest.mark.parametrize("key", list(BOOL_DEFAULTS))
def test_unrecognised_string_returns_default_and_warns(tmp_path, key, caplog):
    with caplog.at_level("WARNING", logger="zotpilot.config"):
        assert _load(tmp_path, key, "bogus") is BOOL_DEFAULTS[key]
    assert "bogus" in caplog.text


@pytest.mark.parametrize("key", list(BOOL_DEFAULTS))
def test_null_returns_default(tmp_path, key):
    assert _load(tmp_path, key, None) is BOOL_DEFAULTS[key]
