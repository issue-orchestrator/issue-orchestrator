"""The exam's engine config must be one production's validator accepts.

The exam hand-writes the tech-lead authority it runs the real engine with.
When the charter (#7330) made ``reset_retry: execute`` a startup error, that
copy went stale and only a live Case B run found out (the engine refused to
start). Loading the block through ``Config.load`` keeps that drift in the
unit gate.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from issue_orchestrator.infra.config import Config
from tests.e2e.exam.scenarios import EXAM_TECH_LEAD_AUTHORITY


def test_exam_tech_lead_authority_loads_through_the_production_validator(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "repo": {"root": str(tmp_path)},
                "tech_lead": {"authority": dict(EXAM_TECH_LEAD_AUTHORITY)},
            }
        )
    )

    config = Config.load(config_file)

    for action, mode in EXAM_TECH_LEAD_AUTHORITY.items():
        assert config.tech_lead.authority.mode_for(action) == mode
