"""Stability and categorization helpers for E2E test results."""

from dataclasses import dataclass

from .e2e_models import E2ETestResult


@dataclass
class TestStability:
    """Flip-rate stability analysis for a single test.

    A "flip" is when a test's outcome changes between consecutive runs
    (pass->fail or fail->pass). High flip rate = flaky.
    """

    __test__ = False  # Not a pytest test class

    nodeid: str
    flip_rate: float  # 0.0 to 1.0
    flip_count: int  # Number of flips in the window
    run_count: int  # Number of runs in the window
    category: str  # flaky, consistently_failing, new_failure, recovered, healthy
    is_likely_flaky: bool  # flip_rate >= threshold
    recent_outcomes: list[str]  # Most recent first: ["passed", "failed", ...]

    @property
    def flip_rate_percent(self) -> float:
        """Flip rate as a percentage (0-100)."""
        return round(self.flip_rate * 100, 1)

    def to_dict(self) -> dict:
        return {
            "nodeid": self.nodeid,
            "flip_rate": self.flip_rate,
            "flip_rate_percent": self.flip_rate_percent,
            "flip_count": self.flip_count,
            "run_count": self.run_count,
            "category": self.category,
            "is_likely_flaky": self.is_likely_flaky,
            "recent_outcomes": self.recent_outcomes,
        }


def _compute_stability(
    nodeid: str,
    outcomes: list[str],
    threshold_percent: float,
) -> TestStability:
    """Compute flip-rate stability for a test from its recent outcomes.

    Pure function, no DB access. Easy to test.

    Args:
        nodeid: Test node ID
        outcomes: Recent outcomes, most recent first (e.g. ["passed", "failed", ...])
        threshold_percent: Flip rate percentage (0-100) above which test is flaky
    """
    if not outcomes:
        return TestStability(
            nodeid=nodeid,
            flip_rate=0.0,
            flip_count=0,
            run_count=0,
            category="healthy",
            is_likely_flaky=False,
            recent_outcomes=[],
        )

    flip_count = 0
    for i in range(1, len(outcomes)):
        if outcomes[i] != outcomes[i - 1]:
            flip_count += 1

    max_transitions = len(outcomes) - 1
    flip_rate = flip_count / max_transitions if max_transitions > 0 else 0.0

    is_likely_flaky = (flip_rate * 100) >= threshold_percent
    category = _categorize_test(outcomes, is_likely_flaky)

    return TestStability(
        nodeid=nodeid,
        flip_rate=flip_rate,
        flip_count=flip_count,
        run_count=len(outcomes),
        category=category,
        is_likely_flaky=is_likely_flaky,
        recent_outcomes=outcomes,
    )


def _categorize_test(outcomes: list[str], is_likely_flaky: bool) -> str:
    """Categorize a test based on its recent outcomes.

    Pure function, no DB access.

    Categories:
        flaky: flip_rate >= threshold
        consistently_failing: low flip rate, most recent is failure, not flaky
        new_failure: < 3 runs of history, recent failure
        recovered: most recent is pass, but had failures in window
        healthy: all passes or no history
    """
    if not outcomes:
        return "healthy"

    if is_likely_flaky:
        return "flaky"

    most_recent = outcomes[0]
    has_failures = any(o == "failed" for o in outcomes)

    if most_recent == "failed":
        if len(outcomes) < 3:
            return "new_failure"
        return "consistently_failing"

    if has_failures:
        return "recovered"

    return "healthy"


def categorize_test_results(
    results: list[E2ETestResult],
    history_by_nodeid: dict[str, list[dict]],
    issues_by_nodeid: dict[str, dict],
    flake_threshold_percent: float,
) -> dict[str, list[dict]]:
    """Categorize test results into groups for the unified run view.

    Categories:
        untriaged: consistently failing, no issue
        has_issue: consistently failing, issue exists
        flaky: unstable history (any outcome this run)
        fixed: passed, has open issue to close
        passed: stable passing
        quarantined: quarantined tests
        skipped: skipped tests
    """
    tests_by_category: dict[str, list[dict]] = {
        "untriaged": [],
        "has_issue": [],
        "flaky": [],
        "fixed": [],
        "passed": [],
        "quarantined": [],
        "skipped": [],
    }

    for result in results:
        test_dict = _build_enhanced_test_dict(
            result, history_by_nodeid, issues_by_nodeid, flake_threshold_percent
        )
        category = _determine_test_category(result, test_dict)
        test_dict["result_category"] = category
        tests_by_category[category].append(test_dict)

    return tests_by_category


def _build_enhanced_test_dict(
    result: E2ETestResult,
    history_by_nodeid: dict[str, list[dict]],
    issues_by_nodeid: dict[str, dict],
    flake_threshold_percent: float,
) -> dict:
    """Build enhanced test dict with history, issue info, and stability data."""
    nodeid = result.nodeid
    test_dict = result.to_dict()

    history = history_by_nodeid.get(nodeid, [])
    test_dict["history"] = history

    existing_issue = issues_by_nodeid.get(nodeid)
    test_dict["existing_issue"] = existing_issue

    effective_outcome = result.retry_outcome or result.outcome
    all_outcomes = [effective_outcome] + [h["outcome"] for h in history]
    stability = _compute_stability(nodeid, all_outcomes, flake_threshold_percent)
    test_dict["category"] = stability.category
    test_dict["flip_rate"] = stability.flip_rate
    test_dict["flip_rate_percent"] = stability.flip_rate_percent
    test_dict["is_likely_flaky"] = stability.is_likely_flaky

    return test_dict


def _determine_test_category(
    result: E2ETestResult,
    test_dict: dict,
) -> str:
    """Determine which category a test belongs to for grouping."""
    if result.is_quarantined:
        return "quarantined"
    if result.outcome == "skipped":
        return "skipped"
    if test_dict["is_likely_flaky"]:
        return "flaky"

    effective_outcome = result.retry_outcome or result.outcome
    existing_issue = test_dict.get("existing_issue")
    has_open_issue = existing_issue and existing_issue["status"] == "open"

    if effective_outcome == "passed":
        return "fixed" if has_open_issue else "passed"
    return "has_issue" if has_open_issue else "untriaged"
