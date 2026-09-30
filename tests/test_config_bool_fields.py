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


@pytest.mark.parametrize("key", ["oa_pdf_upload", "deploy_skills"])
def test_string_false_is_false(tmp_path, key):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({key: "false"}))
    assert getattr(Config.load(path), key) is False
