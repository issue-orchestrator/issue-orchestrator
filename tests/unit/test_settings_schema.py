"""Tests for the data-driven settings schema."""

from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from issue_orchestrator.infra.config import Config, E2EConfig, FilteringConfig
from issue_orchestrator.infra.settings_schema import (
    TAB_DEFINITIONS,
    AdvancedSettings,
    ConcurrencySettings,
    E2ESettings,
    FilteringSettings,
    ReviewSettings,
    apply_to,
    from_config,
    generate_config_reference,
    get_field_meta,
    get_restart_fields,
    get_settings_json_schema,
)


# ---------------------------------------------------------------------------
# Model construction
# ---------------------------------------------------------------------------

class TestModelDefaults:
    """Each model should construct with its declared defaults."""

    def test_concurrency_defaults(self):
        m = ConcurrencySettings()
        assert m.max_concurrent_sessions == 3
        assert m.session_timeout_minutes == 45
        assert m.queue_refresh_seconds == 600

    def test_e2e_defaults(self):
        m = E2ESettings()
        assert m.enabled is False
        assert m.role == "auto"
        assert m.pytest_args == "tests/e2e -v"
        assert m.allow_retry_once is True
        assert m.stop_on_first_failure is False

    def test_filtering_defaults(self):
        m = FilteringSettings()
        assert m.label is None
        assert m.milestones == ""
        assert m.exclude_labels == ""
        assert m.fetch_limit == 100
        assert m.max_to_start == 0

    def test_review_defaults(self):
        m = ReviewSettings()
        assert m.enabled is False
        assert m.default_reviewer is None
        assert m.max_rework_cycles == 2

    def test_advanced_defaults(self):
        m = AdvancedSettings()
        assert m.web_port == 8080
        assert m.control_api_port == 19080
        assert m.worktree_branch_on_recreate == "delete"


# ---------------------------------------------------------------------------
# Pydantic validation
# ---------------------------------------------------------------------------

class TestValidation:
    """Pydantic should reject out-of-range values."""

    def test_max_concurrent_sessions_min(self):
        with pytest.raises(ValidationError):
            ConcurrencySettings(max_concurrent_sessions=0)

    def test_max_concurrent_sessions_max(self):
        with pytest.raises(ValidationError):
            ConcurrencySettings(max_concurrent_sessions=21)

    def test_session_timeout_min(self):
        with pytest.raises(ValidationError):
            ConcurrencySettings(session_timeout_minutes=4)

    def test_session_timeout_max(self):
        with pytest.raises(ValidationError):
            ConcurrencySettings(session_timeout_minutes=181)

    def test_e2e_role_enum(self):
        with pytest.raises(ValidationError):
            E2ESettings(role="invalid")

    def test_e2e_auto_run_interval_max(self):
        with pytest.raises(ValidationError):
            E2ESettings(auto_run_interval_minutes=1441)

    def test_filtering_fetch_limit_min(self):
        with pytest.raises(ValidationError):
            FilteringSettings(fetch_limit=0)

    def test_review_max_rework_max(self):
        with pytest.raises(ValidationError):
            ReviewSettings(max_rework_cycles=11)

    def test_web_port_min(self):
        with pytest.raises(ValidationError):
            AdvancedSettings(web_port=1023)

    def test_web_port_max(self):
        with pytest.raises(ValidationError):
            AdvancedSettings(web_port=65536)

    def test_worktree_branch_on_recreate_enum(self):
        with pytest.raises(ValidationError):
            AdvancedSettings(worktree_branch_on_recreate="invalid")

    def test_valid_values_accepted(self):
        """Boundary values should be accepted."""
        m = ConcurrencySettings(max_concurrent_sessions=1, session_timeout_minutes=5)
        assert m.max_concurrent_sessions == 1
        assert m.session_timeout_minutes == 5

        m2 = AdvancedSettings(web_port=1024, control_api_port=0)
        assert m2.web_port == 1024
        assert m2.control_api_port == 0


# ---------------------------------------------------------------------------
# from_config round-trip
# ---------------------------------------------------------------------------

class TestFromConfig:
    """from_config should read all fields from a Config object."""

    def _make_config(self) -> Config:
        cfg = Config()
        cfg.max_concurrent_sessions = 5
        cfg.session_timeout_minutes = 60
        cfg.queue_refresh_seconds = 300
        cfg.e2e = E2EConfig(
            enabled=True,
            role="executor",
            auto_run_interval_minutes=15,
            pytest_args=["tests/e2e", "-v", "--timeout=30"],
            allow_retry_once=False,
            stop_on_first_failure=True,
            quarantine_file="quarantine.txt",
        )
        cfg.filtering = FilteringConfig(
            label="bot-ready",
            milestones=["M1", "M2"],
            exclude_labels=["test-data", "skip"],
            fetch_limit=50,
            max_to_start=10,
        )
        cfg.review_enabled = True
        cfg.code_review_agent = "agent:reviewer"
        cfg.max_rework_cycles = 3
        cfg.triage_review_agent = "agent:triage"
        cfg.triage_review_threshold = 5
        cfg.session_no_output_seconds = 60
        cfg.stale_escalation_ticks = 3
        cfg.web_port = 9090
        cfg.control_api_port = 19090
        cfg.worktree_base = Path("/tmp/worktrees")
        cfg.worktree_branch_on_recreate = "create_new_branch"
        return cfg

    def test_concurrency_tab(self):
        tabs = from_config(self._make_config())
        conc = tabs["concurrency"]
        assert isinstance(conc, ConcurrencySettings)
        assert conc.max_concurrent_sessions == 5
        assert conc.session_timeout_minutes == 60
        assert conc.queue_refresh_seconds == 300

    def test_e2e_tab(self):
        tabs = from_config(self._make_config())
        e2e = tabs["e2e"]
        assert isinstance(e2e, E2ESettings)
        assert e2e.enabled is True
        assert e2e.role == "executor"
        assert e2e.auto_run_interval_minutes == 15
        # pytest_args list should be joined with spaces
        assert e2e.pytest_args == "tests/e2e -v --timeout=30"
        assert e2e.allow_retry_once is False
        assert e2e.stop_on_first_failure is True
        assert e2e.quarantine_file == "quarantine.txt"

    def test_filtering_tab(self):
        tabs = from_config(self._make_config())
        filt = tabs["filtering"]
        assert isinstance(filt, FilteringSettings)
        assert filt.label == "bot-ready"
        assert filt.milestones == "M1, M2"
        assert filt.exclude_labels == "test-data, skip"
        assert filt.fetch_limit == 50
        assert filt.max_to_start == 10

    def test_review_tab(self):
        tabs = from_config(self._make_config())
        rev = tabs["review"]
        assert isinstance(rev, ReviewSettings)
        assert rev.enabled is True
        assert rev.default_reviewer == "agent:reviewer"
        assert rev.max_rework_cycles == 3
        assert rev.triage_agent == "agent:triage"
        assert rev.triage_threshold == 5

    def test_advanced_tab(self):
        tabs = from_config(self._make_config())
        adv = tabs["advanced"]
        assert isinstance(adv, AdvancedSettings)
        assert adv.session_no_output_seconds == 60
        assert adv.stale_escalation_ticks == 3
        assert adv.web_port == 9090
        assert adv.control_api_port == 19090
        assert adv.worktree_base == "/tmp/worktrees"
        assert adv.worktree_branch_on_recreate == "create_new_branch"

    def test_default_config_round_trips(self):
        """Default Config() should produce valid schema models."""
        cfg = Config()
        tabs = from_config(cfg)
        assert len(tabs) == 5
        for key, model in tabs.items():
            assert model is not None


# ---------------------------------------------------------------------------
# apply_to round-trip
# ---------------------------------------------------------------------------

class TestApplyTo:
    """apply_to should write all fields back to a Config object."""

    def test_round_trip(self):
        """from_config -> apply_to should preserve values."""
        cfg = Config()
        cfg.max_concurrent_sessions = 7
        cfg.session_timeout_minutes = 90
        cfg.e2e = E2EConfig(enabled=True, pytest_args=["tests/e2e", "-v"])
        cfg.filtering = FilteringConfig(milestones=["M1"], exclude_labels=["skip"])
        cfg.review_enabled = True
        cfg.code_review_agent = "agent:rev"

        tabs = from_config(cfg)
        cfg2 = Config()
        apply_to(tabs, cfg2)

        assert cfg2.max_concurrent_sessions == 7
        assert cfg2.session_timeout_minutes == 90
        assert cfg2.e2e.enabled is True
        assert cfg2.e2e.pytest_args == ["tests/e2e", "-v"]
        assert cfg2.filtering.milestones == ["M1"]
        assert cfg2.filtering.exclude_labels == ["skip"]
        assert cfg2.review_enabled is True
        assert cfg2.code_review_agent == "agent:rev"

    def test_restart_required_detection(self):
        """apply_to should return True when restart-required fields change."""
        cfg = Config()
        cfg.web_port = 8080

        tabs = from_config(cfg)
        # Change the web port
        tabs["advanced"] = AdvancedSettings(
            web_port=9090,
            control_api_port=cfg.control_api_port,
            worktree_base=str(cfg.worktree_base),
            worktree_branch_on_recreate=cfg.worktree_branch_on_recreate,
        )
        restart = apply_to(tabs, cfg)
        assert restart is True
        assert cfg.web_port == 9090

    def test_no_restart_when_unchanged(self):
        """apply_to should return False when no restart-required fields change."""
        cfg = Config()
        tabs = from_config(cfg)
        restart = apply_to(tabs, cfg)
        assert restart is False

    def test_comma_separated_list_transform(self):
        """Comma-separated string should be split into list."""
        cfg = Config()
        tabs = from_config(cfg)
        tabs["filtering"] = FilteringSettings(
            milestones="M1, M2, M3",
            exclude_labels="skip, test",
        )
        apply_to(tabs, cfg)
        assert cfg.filtering.milestones == ["M1", "M2", "M3"]
        assert cfg.filtering.exclude_labels == ["skip", "test"]

    def test_space_separated_list_transform(self):
        """Space-separated string should be split into list."""
        cfg = Config()
        tabs = from_config(cfg)
        tabs["e2e"] = E2ESettings(pytest_args="tests/e2e -v --timeout=30")
        apply_to(tabs, cfg)
        assert cfg.e2e.pytest_args == ["tests/e2e", "-v", "--timeout=30"]

    def test_empty_list_transforms(self):
        """Empty strings should produce empty lists."""
        cfg = Config()
        tabs = from_config(cfg)
        tabs["filtering"] = FilteringSettings(milestones="", exclude_labels="")
        tabs["e2e"] = E2ESettings(pytest_args="")
        apply_to(tabs, cfg)
        assert cfg.filtering.milestones == []
        assert cfg.filtering.exclude_labels == []
        assert cfg.e2e.pytest_args == []

    def test_optional_string_none(self):
        """None optional strings should be written as None."""
        cfg = Config()
        tabs = from_config(cfg)
        tabs["filtering"] = FilteringSettings(label=None)
        tabs["review"] = ReviewSettings(default_reviewer=None, triage_agent=None)
        apply_to(tabs, cfg)
        assert cfg.filtering.label is None
        assert cfg.code_review_agent is None
        assert cfg.triage_review_agent is None


# ---------------------------------------------------------------------------
# JSON Schema generation
# ---------------------------------------------------------------------------

class TestJsonSchema:
    """get_settings_json_schema should produce valid per-tab schemas."""

    def test_all_tabs_present(self):
        schemas = get_settings_json_schema()
        expected_keys = {tab["key"] for tab in TAB_DEFINITIONS}
        assert set(schemas.keys()) == expected_keys

    def test_schema_has_properties(self):
        schemas = get_settings_json_schema()
        for key, schema in schemas.items():
            assert "properties" in schema, f"Tab {key} schema missing properties"
            assert len(schema["properties"]) > 0, f"Tab {key} has no properties"

    def test_x_extra_populated(self):
        """Each property should have x_extra with at least config_attr."""
        schemas = get_settings_json_schema()
        for key, schema in schemas.items():
            for prop_name, prop in schema["properties"].items():
                assert "x_extra" in prop, f"{key}.{prop_name} missing x_extra"
                assert "config_attr" in prop["x_extra"], f"{key}.{prop_name} missing config_attr"

    def test_schema_titles(self):
        """Properties should have human-readable titles."""
        schemas = get_settings_json_schema()
        for key, schema in schemas.items():
            for prop_name, prop in schema["properties"].items():
                assert "title" in prop, f"{key}.{prop_name} missing title"

    def test_schema_is_cached(self):
        """Calling get_settings_json_schema twice should return the same object."""
        s1 = get_settings_json_schema()
        s2 = get_settings_json_schema()
        assert s1 is s2


# ---------------------------------------------------------------------------
# Metadata accessor
# ---------------------------------------------------------------------------

class TestGetFieldMeta:

    def test_known_field(self):
        meta = get_field_meta("concurrency", "max_concurrent_sessions")
        assert meta["title"] == "Max Concurrent Sessions"
        assert meta["default"] == 3
        assert meta["yaml_path"] == "execution.concurrency.max_concurrent_sessions"

    def test_unknown_tab_raises(self):
        with pytest.raises(KeyError):
            get_field_meta("nonexistent", "max_concurrent_sessions")

    def test_unknown_field_raises(self):
        with pytest.raises(KeyError):
            get_field_meta("concurrency", "nonexistent_field")


# ---------------------------------------------------------------------------
# Restart fields
# ---------------------------------------------------------------------------

class TestRestartFields:

    def test_restart_fields_present(self):
        fields = get_restart_fields()
        assert "web_port" in fields
        assert "control_api_port" in fields
        assert "worktree_base" in fields
        assert "worktree_branch_on_recreate" in fields

    def test_non_restart_fields_absent(self):
        fields = get_restart_fields()
        assert "max_concurrent_sessions" not in fields
        assert "enabled" not in fields


# ---------------------------------------------------------------------------
# Documentation generation
# ---------------------------------------------------------------------------

class TestDocGeneration:

    def test_generates_markdown(self):
        md = generate_config_reference()
        assert "# Settings Reference" in md
        assert "## Concurrency" in md
        assert "## E2E Runner" in md
        assert "## Filtering" in md
        assert "## Review" in md
        assert "## Advanced" in md

    def test_contains_yaml_paths(self):
        md = generate_config_reference()
        assert "execution.concurrency.max_concurrent_sessions" in md
        assert "e2e.enabled" in md
        assert "ui.web_port" in md

    def test_table_format(self):
        md = generate_config_reference()
        assert "| Field | Type | Default | Description |" in md
        assert "|-------|------|---------|-------------|" in md


# ---------------------------------------------------------------------------
# TAB_DEFINITIONS consistency
# ---------------------------------------------------------------------------

class TestTabDefinitions:

    def test_all_tabs_have_keys(self):
        for tab in TAB_DEFINITIONS:
            assert "key" in tab
            assert "label" in tab
            assert "model" in tab

    def test_all_models_are_basemodel(self):
        for tab in TAB_DEFINITIONS:
            assert issubclass(tab["model"], BaseModel)

    def test_all_fields_have_config_attr(self):
        """Every field in every model must have config_attr in json_schema_extra."""
        for tab in TAB_DEFINITIONS:
            model_cls = tab["model"]
            for field_name, field_info in model_cls.model_fields.items():
                extra = field_info.json_schema_extra
                assert isinstance(extra, dict), (
                    f"{tab['key']}.{field_name} missing json_schema_extra"
                )
                assert "config_attr" in extra, (
                    f"{tab['key']}.{field_name} missing config_attr"
                )

    def test_all_fields_have_yaml_path(self):
        """Every field must have yaml_path for documentation."""
        for tab in TAB_DEFINITIONS:
            model_cls = tab["model"]
            for field_name, field_info in model_cls.model_fields.items():
                extra = field_info.json_schema_extra
                assert isinstance(extra, dict)
                assert "yaml_path" in extra, (
                    f"{tab['key']}.{field_name} missing yaml_path"
                )
