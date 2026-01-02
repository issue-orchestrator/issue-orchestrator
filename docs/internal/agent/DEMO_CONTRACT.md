# Demo contract (mechanical)

Add a Makefile target `demo` OR a CLI command `issue-orchestrator demo`.

**Behavior**:
- If ISSUE_ORCH_GITHUB_TOKEN is not set:
  - print: "DEMO: no token set; running dry-run"
  - run planner against local fixtures (no GitHub writes)
  - exit 0
- If token set and repo configured:
  - create a demo issue with known prefix (e.g. [DEMO-001])
  - trigger one run cycle that reaches draft PR or needs-human
  - print the issue URL and PR URL
  - exit 0

**Gate**: `make demo` exits 0 and prints one of:
- "DEMO: no token set; running dry-run"
- "DEMO: created issue" and "DEMO: opened draft PR"
