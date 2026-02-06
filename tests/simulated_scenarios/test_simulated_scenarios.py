from pathlib import Path

from issue_orchestrator.events import EventName

from .scenario_dsl import scenario, script


def test_local_loop_happy_path_creates_non_draft_pr(scenario_repo: Path):
    scenario("happy_path_local_loop", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .validation(cmd=script("validate_pass.sh")) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_pr(created=True, draft=False) \
        .expect_event(EventName.REVIEW_EXCHANGE_STARTED) \
        .expect_event(EventName.REVIEW_EXCHANGE_COMPLETED) \
        .run()


def test_local_loop_two_rounds_of_review(scenario_repo: Path):
    scenario("two_rounds", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_two_rounds.sh", prompt=True)) \
        .validation(cmd=script("validate_pass.sh")) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_pr(created=True) \
        .expect_review_exchange_rounds(3) \
        .run()


def test_review_exchange_disagree_then_ok(scenario_repo: Path):
    scenario("disagree_then_ok", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_disagree_then_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_review_exchange_reason("reviewer_ok") \
        .run()


def test_review_exchange_noise_output_parses(scenario_repo: Path):
    scenario("noise_output", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_noise_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_review_exchange_round_response(reviewer_response_type="ok") \
        .run()


def test_validation_failure_queues_retry(scenario_repo: Path):
    scenario("validation_retry_queue", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .validation(cmd=script("validate_fail.sh"), max_retries=1) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .wait_for(lambda orch: len(orch.state.pending_validation_retries) > 0, max_ticks=6) \
        .expect_pending_validation_retries(1) \
        .run()


def test_review_exchange_cache_skips_agent_run(scenario_repo: Path):
    ctx1 = scenario("cache_skips_first", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_event(EventName.REVIEW_EXCHANGE_STARTED) \
        .run()
    assert ctx1 is not None

    ctx2 = scenario("cache_skips_second", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_no_event(EventName.REVIEW_EXCHANGE_STARTED) \
        .run()
    assert ctx2 is not None


def test_validation_failure_exhausts_retries(scenario_repo: Path):
    scenario("validation_exhausted", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .validation(cmd=script("validate_fail.sh"), max_retries=0) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_pending_validation_retries(0) \
        .expect_session_history_status({"validation_failed"}) \
        .run()


def test_review_exchange_stops_on_no_progress(scenario_repo: Path):
    scenario("no_progress", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_no_progress.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False, max_no_progress=1) \
        .expect_review_exchange_reason("reviewer_reports_no_progress") \
        .run()


def test_review_exchange_max_rounds_exceeded(scenario_repo: Path):
    scenario("max_rounds", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_never_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False, max_rounds=2, max_no_progress=0) \
        .expect_review_exchange_reason("max_rounds_exceeded") \
        .expect_review_exchange_rounds(2) \
        .run()


def test_review_exchange_requires_validation_blocks_ok(scenario_repo: Path):
    scenario("require_validation_blocks_ok", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=True, max_no_progress=1) \
        .expect_review_exchange_reason("reviewer_reports_no_progress") \
        .run()


def test_review_exchange_requires_validation_allows_ok(scenario_repo: Path):
    scenario("require_validation_allows_ok", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_ok_with_validation.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=True) \
        .expect_review_exchange_reason("reviewer_ok") \
        .run()


def test_reviewer_invalid_json_emits_error(scenario_repo: Path):
    scenario("reviewer_invalid_json", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_invalid_json.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False, max_rounds=1) \
        .expect_review_exchange_round_response(reviewer_response_type="error") \
        .run()


def test_reviewer_exit_nonzero_emits_error(scenario_repo: Path):
    scenario("reviewer_exit_nonzero", scenario_repo) \
        .coder(script("coder_dual_mode.sh")) \
        .reviewer(script("reviewer_exit_nonzero.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False, max_rounds=1) \
        .expect_review_exchange_round_response(reviewer_response_type="error") \
        .run()


def test_coder_invalid_json_emits_error(scenario_repo: Path):
    scenario("coder_invalid_json", scenario_repo) \
        .coder(script("coder_invalid_json.sh")) \
        .reviewer(script("reviewer_never_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False, max_rounds=1) \
        .expect_review_exchange_round_response(coder_response_type="error") \
        .run()


def test_coder_exit_nonzero_emits_error(scenario_repo: Path):
    scenario("coder_exit_nonzero", scenario_repo) \
        .coder(script("coder_exit_nonzero.sh")) \
        .reviewer(script("reviewer_never_ok.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False, max_rounds=1) \
        .expect_review_exchange_round_response(coder_response_type="error") \
        .run()
