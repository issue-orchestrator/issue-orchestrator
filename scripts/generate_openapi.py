"""Generate OpenAPI schema for the web UI endpoints."""

from __future__ import annotations

import json
from pathlib import Path

from issue_orchestrator.entrypoints.web import app


def main() -> None:
    schema = app.openapi()
    output_path = Path("docs/api/openapi.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
