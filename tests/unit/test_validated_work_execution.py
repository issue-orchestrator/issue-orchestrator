"""Public execution capability and complete worker lifetime behavior."""

import asyncio
import copy
import pickle
from threading import Event
from unittest.mock import Mock

import pytest

from issue_orchestrator.domain.validated_work_claim import (
    ClaimSecret,
    ProcessIdentity,
    ValidatedWorkClaim,
)
from issue_orchestrator.domain.validated_work_execution import (
    RecordExecutionBusy,
    RecordExecutionBusyError,
    RecordExecutionToken,
)
from issue_orchestrator.execution.validated_work_execution import (
    LocalValidatedWorkExecutionOwner,
)
from issue_orchestrator.ports.validated_work_store import ValidatedWorkStore
from tests.unit.threading_helpers import join_or_fail, run_in_thread, wait_for_event


def make_claim(record_id: str = "record") -> ValidatedWorkClaim:
    return ValidatedWorkClaim(
        record_id, 1, ClaimSecret(), ProcessIdentity("host", 123, "birth", None)
    )


def test_non_reentrant_reservation_and_token_authentication() -> None:
    owner = LocalValidatedWorkExecutionOwner(Mock(spec=ValidatedWorkStore))
    lease = owner.try_enter("record")
    assert not isinstance(lease, RecordExecutionBusy)
    assert owner.try_enter("record") == RecordExecutionBusy("record")
    with lease as token:
        owner.require_active(token, "record")
        assert owner.try_enter("record") == RecordExecutionBusy("record")
        other = owner.try_enter("other")
        assert not isinstance(other, RecordExecutionBusy)
        with other as other_token:
            owner.require_active(other_token, "other")
            with pytest.raises(RuntimeError):
                owner.require_active(token, "other")
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with pytest.raises(TypeError):
                operation(token)
        foreign = LocalValidatedWorkExecutionOwner(Mock(spec=ValidatedWorkStore))
        with pytest.raises(RuntimeError):
            foreign.claim(token)
        thread, result = run_in_thread(owner.claim, token)
        join_or_fail(thread, 5)
        assert isinstance(result.error, RuntimeError)
    with pytest.raises(RuntimeError):
        owner.claim(token)
    with pytest.raises(RuntimeError):
        with lease:
            pytest.fail("lease cannot be reused")
    with pytest.raises(TypeError):
        RecordExecutionToken()
    error = RecordExecutionBusyError("record")
    assert error.record_id == "record"


def test_refused_release_retains_private_handle_and_exception_releases_lease() -> None:
    store = Mock(spec=ValidatedWorkStore)
    store.relinquish_claim.return_value = False
    owner = LocalValidatedWorkExecutionOwner(store)
    claim = make_claim()
    lease = owner.try_enter("record")
    assert not isinstance(lease, RecordExecutionBusy)
    with pytest.raises(ValueError, match="effect failed"):
        with lease as token:
            with pytest.raises(ValueError, match="another record"):
                owner.remember_claim(token, make_claim("other"))
            owner.remember_claim(token, claim)
            assert not owner.relinquish(token)
            assert owner.claim(token) is claim
            with pytest.raises(RuntimeError, match="retained claim"):
                owner.remember_claim(token, make_claim())
            raise ValueError("effect failed")
    second = owner.try_enter("record")
    assert not isinstance(second, RecordExecutionBusy)
    with second as token:
        assert owner.claim(token) is claim
        store.relinquish_claim.return_value = True
        assert owner.relinquish(token)
        assert owner.claim(token) is None
    assert store.relinquish_claim.call_count == 2


@pytest.mark.asyncio
async def test_cancelled_awaiter_cannot_release_worker_lease() -> None:
    store = Mock(spec=ValidatedWorkStore)
    store.relinquish_claim.return_value = True
    owner = LocalValidatedWorkExecutionOwner(store)
    paused, finish, completed = Event(), Event(), Event()
    claim = make_claim()

    def worker() -> None:
        lease = owner.try_enter("record")
        assert not isinstance(lease, RecordExecutionBusy)
        try:
            with lease as token:
                owner.remember_claim(token, claim)
                owner.require_active(token, "record")
                paused.set()
                wait_for_event(finish, 10)
                owner.require_active(token, "record")
                assert owner.relinquish(token)
        finally:
            completed.set()

    task = asyncio.create_task(asyncio.to_thread(worker))
    try:
        await asyncio.to_thread(wait_for_event, paused, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert owner.try_enter("record") == RecordExecutionBusy("record")
        store.relinquish_claim.assert_not_called()
    finally:
        finish.set()
        await asyncio.to_thread(wait_for_event, completed, 5)
    lease = owner.try_enter("record")
    assert not isinstance(lease, RecordExecutionBusy)
    with lease as token:
        assert owner.claim(token) is None
