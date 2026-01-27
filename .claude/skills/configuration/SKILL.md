---
name: configuration
description: Add or modify YAML configuration options. Use when working on infra/config.py, infra/settings_schema.py, config.example.yaml, docs/user/configuration.md, docs/user/e2e.md, test_config.py, or setup_wizard.py. Ensures all config-related files stay in sync.
---

# Configuration Options

This skill provides guidance for adding or modifying configuration options.

## When to Use

- Adding a new field to a config dataclass in `config.py`
- Adding a new field to the settings schema in `settings_schema.py`
- Modifying config parsing in `_parse_*_config()` functions
- Documenting configuration options

## Files to Keep in Sync

When adding a new **settings-visible** config option (one that appears in the web dashboard settings dialog), you only need to update TWO files:

1. **`src/issue_orchestrator/infra/config.py`**
   - Add field to the appropriate dataclass (e.g., `E2EConfig`, `ReviewConfig`)
   - Update the corresponding `_parse_*_config()` function

2. **`src/issue_orchestrator/infra/settings_schema.py`**
   - Add field to the appropriate Pydantic model (e.g., `E2ESettings`, `ReviewSettings`)
   - Set `json_schema_extra` with `config_attr`, `yaml_path`, `section`, etc.

The schema drives everything else automatically:
- **settings.html** — renders from schema (no template changes needed)
- **GET/POST /api/settings** — serializes/validates via Pydantic (no web.py changes needed)
- **setup_wizard.py** — pulls defaults/labels from `get_field_meta()` (if the wizard uses the field)
- **docs/user/configuration.md** — regenerate via `generate_config_reference()`

**Also update (when relevant):**

3. **`examples/config.example.yaml`** — Add the option with a sensible default and comment
4. **`tests/unit/test_config.py`** — Add tests for parsing the new option
5. **`docs/user/e2e.md`** — For e2e-specific options

## Example

Adding `stop_on_first_failure` to E2E config:

```python
# config.py - dataclass
@dataclass
class E2EConfig:
    ...
    stop_on_first_failure: bool = False

# config.py - parser
def _parse_e2e_config(data: dict) -> E2EConfig:
    return E2EConfig(
        ...
        stop_on_first_failure=data.get("stop_on_first_failure", False),
    )
```

```python
# settings_schema.py - Pydantic model
class E2ESettings(BaseModel):
    ...
    stop_on_first_failure: bool = Field(
        False,
        title="Stop on first failure",
        description="Add -x flag to stop test run on first failure",
        json_schema_extra={
            "config_attr": "e2e.stop_on_first_failure",
            "yaml_path": "e2e.stop_on_first_failure",
        },
    )
```

```yaml
# examples/config.example.yaml
e2e:
  stop_on_first_failure: false  # If true, stop on first test failure
```
