# Events

**Purpose**: Event definitions and catalog - the vocabulary for system observability.

**Boundaries**:
- `catalog.py` is the source of truth for all `EventName` constants
- Events are for machines (UI, tests, automation) - use structured, stable schemas
- Logs are for humans - can change freely
- All events must use `EventName` constants, never raw strings
