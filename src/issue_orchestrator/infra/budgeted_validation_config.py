"""Strict YAML schema for named budgeted validation suites.

Named suites are YAML-managed; scalar settings forms do not flatten or overwrite
this collection. The same schema parses runtime config and documents its fields.
"""

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..domain.budgeted_validation import BudgetedValidationSuite, ValidationCadence

PositiveInt = Annotated[int, Field(strict=True, gt=0)]
NonemptyArgument = Annotated[str, Field(strict=True, min_length=1)]


class ValidationCadenceSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_merges_since_success: PositiveInt = Field(10, description="Run when this many PRs have merged since the last successful suite run.")
    max_delay_hours: PositiveInt = Field(24, description="Maximum hours since successful coverage before changed code is due, even below the merge threshold.")


class BudgetedValidationSuiteSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(True, strict=True, description="Enable automatic execution of this configured suite.")
    issue_agent_label: str = Field("agent:backend", strict=True, min_length=1, description="Configured coding-agent label for regression issues; the tech lead champions completion.")
    branch: str = Field("main", strict=True, min_length=1, description="Remote branch whose exact commits are tested and bisected.")
    command: list[NonemptyArgument] = Field(min_length=1, description="Test command as an argument list, executed in an isolated checkout.")
    setup_command: list[NonemptyArgument] = Field(default_factory=list, description="Optional dependency setup command, run in each isolated checkout before tests.")
    timeout_seconds: PositiveInt = Field(3600, description="Deadline for one test command, including a bisection probe.")
    setup_timeout_seconds: PositiveInt = Field(900, description="Deadline for dependency setup in one checkout.")
    cadence: ValidationCadenceSchema = Field(default_factory=lambda: ValidationCadenceSchema.model_validate({}))

    @field_validator("branch")
    @classmethod
    def valid_branch(cls, value: str) -> str:
        if value.startswith(("-", "/")) or any(token in value for token in ("..", "@{", "\\", " ", "\n", ":", "~", "^", "?", "*", "[")):
            raise ValueError("branch must be a plain Git branch name")
        return value

    def to_domain(self, name: str) -> BudgetedValidationSuite:
        return BudgetedValidationSuite(
            name=name, command=tuple(self.command), setup_command=tuple(self.setup_command),
            cadence=ValidationCadence(**self.cadence.model_dump()),
            timeout_seconds=self.timeout_seconds, setup_timeout_seconds=self.setup_timeout_seconds,
            branch=self.branch, enabled=self.enabled, issue_agent_label=self.issue_agent_label,
        )


def parse_budgeted_validation(data: object) -> dict[str, BudgetedValidationSuite]:
    if not isinstance(data, dict):
        raise ValueError("validation.budgeted must map suite names to suite configuration")
    suites: dict[str, BudgetedValidationSuite] = {}
    for name, settings in data.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            raise ValueError("validation.budgeted suite names must be lowercase identifiers (1-64 characters)")
        suites[name] = BudgetedValidationSuiteSchema.model_validate(settings).to_domain(name)
    return suites


def serialize_budgeted_validation(suites: dict[str, BudgetedValidationSuite]) -> dict[str, dict]:
    from dataclasses import asdict

    return {
        name: {**{key: value for key, value in asdict(suite).items() if key != "name"},
               "command": list(suite.command), "setup_command": list(suite.setup_command)}
        for name, suite in suites.items()
    }


def validate_budgeted_validation_agents(suites: dict[str, BudgetedValidationSuite], agents: set[str]) -> list[str]:
    return [
        f"validation.budgeted.{suite.name}.issue_agent_label must reference a configured agent"
        for suite in suites.values() if suite.enabled and suite.issue_agent_label not in agents
    ]


def budgeted_validation_reference() -> str:
    lines = [
        "## Budgeted validation (YAML)", "",
        "Named suites live under `validation.budgeted.<suite>`. An empty mapping disables this workload. "
        "Each suite has its own cadence; either threshold makes changed code due. "
        "Only successful runs advance coverage. No bisection-depth setting is needed.", "",
        "The orchestrator stores each reserved run with its exact command, branch, and",
        "timeouts. A restart resumes that definition even if current configuration",
        "changes, disables, or removes the suite. A pending run reports unavailable",
        "coverage; an older pass cannot make it green. Completed diagnostic steps also",
        "resume from their durable boundary, and confirmed regressions remain visible to",
        "the tech lead after their suite is removed. Runtime requests, histories,",
        "reports, and run evidence use separate storage namespaces so one kind of file",
        "cannot be interpreted as another.", "",
        "| Field | Default | Meaning |", "| --- | --- | --- |",
    ]
    for prefix, model in (("", BudgetedValidationSuiteSchema), ("cadence.", ValidationCadenceSchema)):
        for name, definition in model.model_json_schema()["properties"].items():
            if name == "cadence":
                continue
            default = definition.get("default", "required" if name == "command" else "[]")
            lines.append(f"| `{prefix}{name}` | `{default}` | {definition['description']} |")
    return "\n".join(lines) + "\n"
