**Audience:** Design document (public). Not a usage guide.

# Validation Configuration (YAML)

```yaml
validation:
  publish_gate:
    cmd: "make validate"
    timeout_seconds: 1800
  agent_gate:
    cmd: "make validate-fast"
    timeout_seconds: 600

validation_policy:
  agent_runs: "agent_gate"
  publish_requires: "publish_gate"

isolation:
  mode: "standard"   # or "hardened"
```
