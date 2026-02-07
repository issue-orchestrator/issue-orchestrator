"""Generate UI OpenAPI artifacts from the canonical schema."""

from __future__ import annotations

from issue_orchestrator.contracts.ui_openapi_generator import generate_artifacts


def main() -> None:
    generate_artifacts()


if __name__ == "__main__":
    main()
