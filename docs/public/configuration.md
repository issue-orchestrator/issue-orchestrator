# Configuration

This file describes the user-facing configuration keys.

See `../design/validation_model.md` and `../design/security_isolation.md` for the underlying design intent.

## Validation (publish gate)

```yaml
validation:
  publish_gate:
    cmd: "make validate"
    timeout_seconds: 1800

validation_policy:
  publish_requires: "publish_gate"
```

## Optional fast feedback for agents

```yaml
validation:
  agent_gate:
    cmd: "make validate-fast"
    timeout_seconds: 600
  publish_gate:
    cmd: "make validate"
    timeout_seconds: 1800

validation_policy:
  agent_runs: "agent_gate"
  publish_requires: "publish_gate"
```

## Isolation

```yaml
isolation:
  mode: "standard"   # or "hardened"
```
