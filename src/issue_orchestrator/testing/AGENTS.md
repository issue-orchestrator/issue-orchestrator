# Testing

**Purpose**: Test utilities, fixtures, and helpers shared across test suites.

**Boundaries**:
- Support code for tests, not production code
- `asyncdsl/` contains async test DSL helpers
- `support/` contains fixtures, fakes, and test data builders
- Imported by `tests/` - never imported by production code
