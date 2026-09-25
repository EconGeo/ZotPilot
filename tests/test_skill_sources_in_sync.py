"""The two copies of every packaged skill must be byte-identical.

``claude-skills/<name>/`` is what a user copies by hand (README) and what
downstream repos vendor; ``src/zotpilot/skills/`` is what the wheel ships and
``zotpilot setup`` deploys.  They have drifted before (fixed by hand in
6cb4f48).  This test makes the drift a failing build instead.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PACKAGED = REPO / "src" / "zotpilot" / "skills"
SOURCE = REPO / "claude-skills"


def _packaged_skills() -> list[Path]:
    return sorted(p for p in PACKAGED.glob("*.md") if p.is_file())


@pytest.mark.parametrize("skill_file", _packaged_skills(), ids=lambda p: p.stem)
def test_skill_md_matches_claude_skills_copy(skill_file: Path):
    source = SOURCE / skill_file.stem / "SKILL.md"
    assert source.is_file(), f"{source} missing — every packaged skill needs a claude-skills/ copy"
    assert skill_file.read_bytes() == source.read_bytes(), f"{skill_file.name} drifted from {source}"


@pytest.mark.parametrize("skill_file", _packaged_skills(), ids=lambda p: p.stem)
def test_reference_files_match_claude_skills_copy(skill_file: Path):
    packaged_refs = skill_file.with_suffix("") / "references"
    source_refs = SOURCE / skill_file.stem / "references"
    packaged = sorted(p.name for p in packaged_refs.glob("*.md")) if packaged_refs.is_dir() else []
    source = sorted(p.name for p in source_refs.glob("*.md")) if source_refs.is_dir() else []
    assert packaged == source, f"{skill_file.stem}: reference files differ ({packaged} vs {source})"
    for name in packaged:
        assert (packaged_refs / name).read_bytes() == (source_refs / name).read_bytes(), (
            f"{skill_file.stem}/references/{name} drifted"
        )
