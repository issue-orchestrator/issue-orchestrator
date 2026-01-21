---
name: configuration
description: Add or modify YAML configuration options. Use when working on infra/config.py, config.example.yaml, docs/user/configuration.md, docs/user/e2e.md, test_config.py, or setup_wizard.py. Ensures all config-related files stay in sync.
---

# Configuration Options

This skill provides guidance for adding or modifying configuration options.

## When to Use

- Adding a new field to a config dataclass in `config.py`
- Modifying config parsing in `_parse_*_config()` functions
- Documenting configuration options

## Files to Keep in Sync

When adding a new config option, update ALL of these:

1. **`src/issue_orchestrator/infra/config.py`**
   - Add field to the appropriate dataclass (e.g., `E2EConfig`, `ReviewConfig`)
   - Update the corresponding `_parse_*_config()` function

2. **`examples/config.example.yaml`**
   - Add the option with a sensible default and comment

3. **`docs/user/configuration.md`**
   - Document the option in the appropriate section

4. **`tests/unit/test_config.py`**
   - Add tests for parsing the new option (default value, explicit values)

**Also consider (depending on the option):**

5. **`docs/user/e2e.md`** - For e2e-specific options
6. **`src/issue_orchestrator/entrypoints/cli_tools/setup_wizard.py`** - If users should configure it during initial setup

## Example

Adding `stop_on_first_failure` to E2E config:

```python
# config.py - dataclass
@dataclass
class E2EConfig:
    ...
    stop_on_first_failure: bool = False  # Comment explaining the option

# config.py - parser
def _parse_e2e_config(data: dict) -> E2EConfig:
    return E2EConfig(
        ...
        stop_on_first_failure=data.get("stop_on_first_failure", False),
    )
```

```yaml
# examples/config.example.yaml
e2e:
  stop_on_first_failure: false  # If true, stop on first test failure
```
