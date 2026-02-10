
from tests.e2e.fixtures.cleanup import verify_cleanup_items


def test_cleanup_verify_retries_until_consistent():
    counts: dict[int, int] = {}

    def _check(item: int) -> bool:
        counts[item] = counts.get(item, 0) + 1
        return counts[item] > 1

    remaining = verify_cleanup_items(
        "test",
        [1, 2],
        _check,
        retries=1,
        retry_delay_s=0.0,
    )

    assert remaining == 0
    assert counts == {1: 2, 2: 2}


def test_cleanup_verify_reports_remaining_items():
    def _check(_item: int) -> bool:
        return False

    remaining = verify_cleanup_items(
        "test",
        [1, 2, 3],
        _check,
        retries=1,
        retry_delay_s=0.0,
    )

    assert remaining == 3
