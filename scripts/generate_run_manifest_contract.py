"""Generate JSON schema for run manifest contract."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _bootstrap_imports(base_dir: Path) -> None:
    src_path = base_dir / "src"
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))


def main() -> int:
    base_dir = Path(__file__).resolve().parents[1]
    _bootstrap_imports(base_dir)

    from issue_orchestrator.contracts.run_manifest import run_manifest_json_schema

    schema = run_manifest_json_schema()
    out_path = base_dir / "contracts" / "run-manifest.schema.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(schema, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
