# Adapter Boundary Guardrail

## Overview

This document describes the adapter boundary guardrail, which enforces the hexagonal (ports/adapters) architecture by preventing non-adapter code from accessing adapter-internal implementation details.

## Architecture

The issue-orchestrator uses a hexagonal architecture with clear separation between:

- **Ports** (`ports/`): Protocol definitions (abstract interfaces)
- **Adapters** (`adapters/`, `execution/`): Concrete implementations
- **Core** (`control/`, `observation/`, `domain/`): Business logic that depends only on ports

## The Problem

Non-adapter code should only depend on ports (protocols), not on concrete adapter implementations. For example:

**❌ BAD - Direct access to adapter internals:**
```python
# In entrypoints/bootstrap.py
from issue_orchestrator.adapters.github.http_client import GitHubHttpClient

e2e_tracker = GitHubE2EIssueTracker(github.http_client)  # accessing adapter internals!
```

**✅ GOOD - Use port interfaces:**
```python
# E2EIssueTracker is initialized through a proper port/interface
e2e_tracker = GitHubE2EIssueTracker(client)  # client passed through port
```

## The Guardrail

The adapter boundary guardrail (`validation/adapter_boundary_guardrail.py`) detects two types of violations:

### 1. Adapter-Internal Imports

Prevent importing adapter-specific classes outside `execution/` and `adapters/`:

```python
# ❌ VIOLATION: Importing adapter-internal class in control code
from issue_orchestrator.adapters.github.http_client import GitHubHttpClient

# ✅ OK: Importing from ports
from issue_orchestrator.ports.repository_host import RepositoryHost
```

**Flagged classes** (adapter internals):
- `GitHubHttpClient`
- `GitHubCache`
- `GitHubIssueResolver`
- Others as needed

### 2. Private Attribute Access

Prevent accessing private attributes (`_xxx`) on adapter instances:

```python
# ❌ VIOLATION: Direct access to private attribute
client = github._http_client

# ✅ OK: Use public methods
labels = github.get_issue_labels(issue_number)
```

## Running the Guardrail

### As a Unit Test

The guardrail includes a test suite that can detect violations in your code:

```bash
pytest tests/unit/test_adapter_boundary_guardrail.py -v
```

### Programmatically

```python
from issue_orchestrator.validation import check_adapter_boundaries
from pathlib import Path

result = check_adapter_boundaries(Path("src/issue_orchestrator"))
if result.status == "fail":
    for violation in result.violations:
        print(f"{violation.file_path}:{violation.line_number} {violation.message}")
```

## Allowed Locations

The following packages are exempt from boundary checks (because they implement the adapters):

- `issue_orchestrator.execution.*`
- `issue_orchestrator.adapters.*`

These locations are allowed to import and use adapter-internal classes.

## Checked Locations

The following packages are checked for violations:

- `issue_orchestrator.control.*` - Decision logic
- `issue_orchestrator.entrypoints.*` - Entry points (CLI, HTTP API)
- `issue_orchestrator.observation.*` - Fact gathering
- `issue_orchestrator.domain.*` - Domain models
- `issue_orchestrator.infra.*` - Infrastructure

## How to Fix Violations

If you get a violation, you have two options:

### Option 1: Use a Port (Recommended)

Expose the needed functionality through a proper port interface:

```python
# ports/my_adapter_port.py
class MyAdapterPort(Protocol):
    def get_internal_thing(self) -> SomeType:
        ...

# adapters/my_adapter.py
class MyAdapter(MyAdapterPort):
    def get_internal_thing(self) -> SomeType:
        return self._internal_thing
```

### Option 2: Pass the Dependency

Inject the needed dependency through the port interface:

```python
# adapters/github/e2e_tracker.py
class GitHubE2EIssueTracker(E2EIssueTracker):
    def __init__(self, client: GitHubHttpClient):
        self._client = client

# entrypoints/bootstrap.py
# Instead of: e2e_tracker = GitHubE2EIssueTracker(github.http_client)
# Create a port method that returns the tracker
e2e_tracker = github.create_e2e_issue_tracker()
```

## Future Extensions

The guardrail can be extended to flag additional violations:

- Accessing specific private attributes by name pattern
- Importing adapter test fixtures in non-adapter tests
- Cross-adapter dependencies that should go through ports
- Framework-specific implementation details (pluggy, etc.)

## Related

- [Architecture Guide](./README.md) - Hexagonal architecture overview
- [Ports & Adapters Pattern](./README.md#ports--adapters) - Design principles
