"""Tests for the shared shell secrets file (~/.secrets.env)."""

from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace

import pytest

from zotpilot.config import Config
from zotpilot.credential_migration import migrate_secrets
from zotpilot.runtime_settings import resolve_runtime_settings
from zotpilot.secrets_env import (
    describe_env_file,
    load_env_file,
    set_env_secret,
    unset_env_secret,
)


def _write_secrets(tmp_path, body: str, mode: int = 0o600):
    path = tmp_path / "secrets.env"
    path.write_text(body, encoding="utf-8")
    os.chmod(path, mode)
    return path


class TestParsing:
    def test_reads_export_lines(self, tmp_path):
        path = _write_secrets(tmp_path, 'export ZOTERO_API_KEY="abc123"\n')
        assert load_env_file(path) == {"ZOTERO_API_KEY": "abc123"}

    def test_reads_bare_assignment(self, tmp_path):
        path = _write_secrets(tmp_path, "ZOTERO_API_KEY=abc123\n")
        assert load_env_file(path) == {"ZOTERO_API_KEY": "abc123"}

    def test_single_quotes_and_comments(self, tmp_path):
        path = _write_secrets(
            tmp_path,
            "# a comment\n\nexport A='one'\n  export B=\"two\"\n",
        )
        assert load_env_file(path) == {"A": "one", "B": "two"}

    def test_escapes_inside_double_quotes(self, tmp_path):
        path = _write_secrets(tmp_path, 'export A="a\\"b\\\\c\\$d"\n')
        assert load_env_file(path) == {"A": 'a"b\\c$d'}

    def test_value_is_not_expanded(self, tmp_path):
        """The file is parsed, never executed."""
        path = _write_secrets(tmp_path, 'export A="$(echo pwned)"\n')
        assert load_env_file(path) == {"A": "$(echo pwned)"}

    def test_ignores_non_assignment_lines(self, tmp_path):
        path = _write_secrets(tmp_path, "if [ -f x ]; then\nexport A=1\nfi\n")
        assert load_env_file(path) == {"A": "1"}

    def test_missing_file_is_empty_not_an_error(self, tmp_path):
        assert load_env_file(tmp_path / "nope.env") == {}

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_group_readable_file_is_refused(self, tmp_path):
        path = _write_secrets(tmp_path, "export A=1\n", mode=0o644)
        assert load_env_file(path) == {}
        info = describe_env_file(path)
        assert not info.readable
        assert "chmod 600" in info.detail


class TestWriting:
    def test_appends_new_export(self, tmp_path):
        path = tmp_path / "secrets.env"
        set_env_secret("ZOTERO_API_KEY", "abc123", path)
        assert load_env_file(path)["ZOTERO_API_KEY"] == "abc123"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_new_file_is_owner_only(self, tmp_path):
        path = tmp_path / "secrets.env"
        set_env_secret("A", "1", path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_updates_in_place_and_preserves_neighbours(self, tmp_path):
        path = _write_secrets(
            tmp_path,
            "# keep me\nexport OTHER_KEY=\"untouched\"\nexport ZOTERO_API_KEY=\"old\"\nexport TRAILING=\"z\"\n",
        )
        set_env_secret("ZOTERO_API_KEY", "new", path)
        text = path.read_text(encoding="utf-8")
        assert "# keep me" in text
        values = load_env_file(path)
        assert values == {
            "OTHER_KEY": "untouched",
            "ZOTERO_API_KEY": "new",
            "TRAILING": "z",
        }

    def test_keeps_bare_assignment_style(self, tmp_path):
        path = _write_secrets(tmp_path, "A=old\n")
        set_env_secret("A", "new", path)
        assert path.read_text(encoding="utf-8").strip() == 'A="new"'

    def test_backs_up_before_writing(self, tmp_path):
        path = _write_secrets(tmp_path, 'export A="old"\n')
        set_env_secret("A", "new", path)
        backup = path.with_suffix(path.suffix + ".bak")
        assert load_env_file(backup) == {"A": "old"}

    def test_value_with_quotes_round_trips(self, tmp_path):
        path = tmp_path / "secrets.env"
        tricky = 'a"b\\c$d`e'
        set_env_secret("A", tricky, path)
        assert load_env_file(path)["A"] == tricky

    def test_unset_removes_the_line(self, tmp_path):
        path = _write_secrets(tmp_path, 'export A="1"\nexport B="2"\n')
        assert unset_env_secret("A", path) is True
        assert load_env_file(path) == {"B": "2"}

    def test_unset_reports_when_absent(self, tmp_path):
        path = _write_secrets(tmp_path, 'export B="2"\n')
        assert unset_env_secret("A", path) is False


class TestResolutionPrecedence:
    """config.json < legacy store < secrets file < process env < CLI override."""

    def _config(self, tmp_path, **values):
        path = tmp_path / "config.json"
        path.write_text(json.dumps(values), encoding="utf-8")
        return path

    def test_secrets_file_beats_config_json(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
        cfg = self._config(tmp_path, zotero_api_key="from-config")
        env_path = _write_secrets(tmp_path, 'export ZOTERO_API_KEY="from-file"\n')
        monkeypatch.setenv("ZOTPILOT_ENV_FILE", str(env_path))

        resolved = resolve_runtime_settings(cfg)
        assert resolved.config.zotero_api_key == "from-file"
        assert resolved.sources["zotero_api_key"] == "secrets-env"

    def test_process_env_beats_secrets_file(self, tmp_path, monkeypatch):
        cfg = self._config(tmp_path)
        env_path = _write_secrets(tmp_path, 'export ZOTERO_API_KEY="from-file"\n')
        monkeypatch.setenv("ZOTPILOT_ENV_FILE", str(env_path))
        monkeypatch.setenv("ZOTERO_API_KEY", "from-process")

        resolved = resolve_runtime_settings(cfg)
        assert resolved.config.zotero_api_key == "from-process"
        assert resolved.sources["zotero_api_key"] == "env-override"

    def test_cli_override_beats_everything(self, tmp_path, monkeypatch):
        cfg = self._config(tmp_path, zotero_api_key="from-config")
        env_path = _write_secrets(tmp_path, 'export ZOTERO_API_KEY="from-file"\n')
        monkeypatch.setenv("ZOTPILOT_ENV_FILE", str(env_path))
        monkeypatch.setenv("ZOTERO_API_KEY", "from-process")

        resolved = resolve_runtime_settings(cfg, overrides={"zotero_api_key": "from-cli"})
        assert resolved.config.zotero_api_key == "from-cli"

    def test_config_json_still_works_when_nothing_else_is_set(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
        cfg = self._config(tmp_path, zotero_api_key="from-config")
        monkeypatch.setenv("ZOTPILOT_ENV_FILE", str(tmp_path / "absent.env"))

        resolved = resolve_runtime_settings(cfg)
        assert resolved.config.zotero_api_key == "from-config"
        assert resolved.sources["zotero_api_key"] == "config"

    def test_semantic_scholar_alias_is_accepted(self, tmp_path, monkeypatch):
        monkeypatch.delenv("S2_API_KEY", raising=False)
        monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
        cfg = self._config(tmp_path)
        env_path = _write_secrets(tmp_path, 'export SEMANTIC_SCHOLAR_API_KEY="s2-key"\n')
        monkeypatch.setenv("ZOTPILOT_ENV_FILE", str(env_path))

        resolved = resolve_runtime_settings(cfg)
        assert resolved.config.semantic_scholar_api_key == "s2-key"

    def test_canonical_name_wins_over_its_alias(self, tmp_path, monkeypatch):
        monkeypatch.delenv("S2_API_KEY", raising=False)
        monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
        cfg = self._config(tmp_path)
        env_path = _write_secrets(
            tmp_path,
            'export SEMANTIC_SCHOLAR_API_KEY="alias"\nexport S2_API_KEY="canonical"\n',
        )
        monkeypatch.setenv("ZOTPILOT_ENV_FILE", str(env_path))

        resolved = resolve_runtime_settings(cfg)
        assert resolved.config.semantic_scholar_api_key == "canonical"


class TestConfigSaveDropsSecrets:
    def test_save_never_writes_a_key(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps({"zotero_api_key": "leaked", "chunk_size": 321}), encoding="utf-8"
        )
        config = Config.load(cfg_path)
        assert config.zotero_api_key == "leaked"  # still read, for compatibility

        config.save(cfg_path)

        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert "zotero_api_key" not in data
        assert data["chunk_size"] == 321


class TestMigration:
    def test_moves_config_json_key_into_the_secrets_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
        monkeypatch.setattr(
            "zotpilot._platforms.reconcile_runtime",
            lambda **kwargs: SimpleNamespace(applied=None),
            raising=True,
        )
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps({"zotero_api_key": "abc123", "chunk_size": 321}), encoding="utf-8"
        )
        env_path = tmp_path / "secrets.env"

        result = migrate_secrets(config_path=cfg_path, env_file=env_path)

        assert load_env_file(env_path)["ZOTERO_API_KEY"] == "abc123"
        assert result.removed_from_config == ["zotero_api_key"]
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert "zotero_api_key" not in data
        assert data["chunk_size"] == 321

    def test_does_not_overwrite_an_existing_entry(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ZOTERO_API_KEY", raising=False)
        monkeypatch.setattr(
            "zotpilot._platforms.reconcile_runtime",
            lambda **kwargs: SimpleNamespace(applied=None),
            raising=True,
        )
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"zotero_api_key": "from-config"}), encoding="utf-8")
        env_path = _write_secrets(tmp_path, 'export ZOTERO_API_KEY="already-here"\n')

        result = migrate_secrets(config_path=cfg_path, env_file=env_path)

        assert load_env_file(env_path)["ZOTERO_API_KEY"] == "already-here"
        assert "zotero_api_key" in result.preserved

    def test_does_not_capture_the_process_environment(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "zotpilot._platforms.reconcile_runtime",
            lambda **kwargs: SimpleNamespace(applied=None),
            raising=True,
        )
        monkeypatch.setenv("GEMINI_API_KEY", "runtime-only")
        cfg_path = tmp_path / "config.json"
        env_path = tmp_path / "secrets.env"

        migrate_secrets(config_path=cfg_path, env_file=env_path)

        assert "GEMINI_API_KEY" not in load_env_file(env_path)
