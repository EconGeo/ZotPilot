"""deploy_skills=false: reconcile neither deploys nor reports drift for missing skills,
and removes only the skill dirs ZotPilot itself deployed (real dir + version marker)."""
from __future__ import annotations

import json
from unittest.mock import patch

from zotpilot._platforms import (
    DesiredRuntime,
    PlatformRuntimeState,
    RuntimeState,
    _skill_deploy_enabled,
    _skill_source_files,
    plan_runtime_changes,
    reconcile_runtime,
    undeploy_skills,
)


def _state(**kw) -> RuntimeState:
    base = dict(platform="claude-code", label="Claude Code", supported=True, detected=True,
                registered=True, command="/usr/bin/zotpilot", args=("mcp", "serve"), env={},
                skill_hash_ok=False)
    base.update(kw)
    return RuntimeState("0.5.0", ("claude-code",), {"claude-code": PlatformRuntimeState(**base)})


def _desired(deploy: bool) -> DesiredRuntime:
    return DesiredRuntime(command="/usr/bin/zotpilot", args=("mcp", "serve"), env={},
                          targets=("claude-code",), deploy_skills=deploy)


def test_disabled_and_absent_is_clean():
    changes = plan_runtime_changes(_desired(False), _state())
    assert changes.deploy_skill_platforms == ()
    assert changes.undeploy_skill_platforms == ()
    assert changes.drift_state == "clean"


def test_disabled_with_deployed_copies_undeploys():
    changes = plan_runtime_changes(_desired(False), _state(managed_skill_dirs=("/h/.claude/skills/ztp-research",)))
    assert changes.undeploy_skill_platforms == ("claude-code",)
    assert changes.deploy_skill_platforms == ()
    assert changes.reasons["claude-code"] == ["skills-deployed-while-disabled"]
    assert changes.drift_state == "needs-sync"


def test_enabled_behaviour_unchanged():
    changes = plan_runtime_changes(_desired(True), _state())
    assert changes.deploy_skill_platforms == ("claude-code",)
    assert changes.undeploy_skill_platforms == ()


def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / ".claude" / "skills"


def test_undeploy_removes_only_marked_real_dirs(tmp_path, monkeypatch):
    skills = _home(tmp_path, monkeypatch)
    names = [p.stem for p in _skill_source_files()]
    marked, unmarked, linked = names[0], names[1], names[2]
    (skills / marked).mkdir(parents=True)
    (skills / marked / "SKILL.md").write_text("x")
    (skills / marked / ".zotpilot-version.json").write_text("{}")
    (skills / unmarked).mkdir()
    (skills / unmarked / "SKILL.md").write_text("hand-made")
    elsewhere = tmp_path / "vendored" / linked
    elsewhere.mkdir(parents=True)
    (elsewhere / ".zotpilot-version.json").write_text("{}")
    (skills / linked).symlink_to(elsewhere)

    assert undeploy_skills(["claude-code"]) == {"claude-code": True}
    assert not (skills / marked).exists()
    assert (skills / unmarked / "SKILL.md").read_text() == "hand-made"
    assert (skills / linked).is_symlink() and elsewhere.is_dir()


def _write_config(tmp_path, data):
    cfg = tmp_path / ".config" / "zotpilot" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps(data))
    return cfg


def test_deploy_setting_read_despite_invalid_config(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    cfg = _write_config(tmp_path, {"deploy_skills": False, "chunker_backend": "bogus"})
    assert _skill_deploy_enabled(cfg) is False


def test_deploy_setting_defaults_true(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    assert _skill_deploy_enabled(tmp_path / "nope.json") is True


def test_reconcile_end_to_end_plans_undeploy(tmp_path, monkeypatch):
    skills = _home(tmp_path, monkeypatch)
    _write_config(tmp_path, {"deploy_skills": False})
    name = _skill_source_files()[0].stem
    (skills / name).mkdir(parents=True)
    (skills / name / ".zotpilot-version.json").write_text("{}")
    from zotpilot._platforms import _runtime_invocation
    cmd, args = _runtime_invocation()
    with (
        patch("zotpilot._platforms.detect_platforms", return_value=["claude-code"]),
        patch("zotpilot._platforms._inspect_registration", return_value=(True, cmd, args, {}, None)),
    ):
        result = reconcile_runtime(platforms=["claude-code"], apply=False)
    assert result.desired.deploy_skills is False
    assert result.changes.undeploy_skill_platforms == ("claude-code",)
    assert result.changes.deploy_skill_platforms == ()
