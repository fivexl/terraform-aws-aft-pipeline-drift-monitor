"""Tests for the weekly full-run Lambda."""

from __future__ import annotations

import pytest

import run_all
from tests.conftest import STATE_MACHINE_ARN

EVENT = {"id": "cdc73f9d-aea9-11e3-9d5a-835b769c0d9c"}


@pytest.fixture(autouse=True)
def wire_clients(monkeypatch, sfn, sns):
    monkeypatch.setattr(run_all, "stepfunctions", sfn)
    monkeypatch.setattr(run_all, "sns", sns)


def only_execution(sfn) -> dict:
    assert len(sfn.executions) == 1, sfn.executions
    return sfn.executions[0]


def test_every_aft_account_is_handed_to_aft_in_one_execution(sfn, sns):
    """"Every account" is AFT's answer, resolved from its metadata table.

    The old handler listed pipelines by name pattern and read ten executions of
    each. include: all covers every AFT-managed account instead - including one
    whose pipeline the pattern would have missed.
    """
    summary = run_all.lambda_handler(EVENT, None)

    execution = only_execution(sfn)
    assert execution["stateMachineArn"] == STATE_MACHINE_ARN
    assert execution["input"] == {
        "include": [{"type": "all"}],
        "bypass_steps": ["provisioning_bootstrap"],
    }
    assert summary["already_invoked"] is False
    assert summary["invoke_error"] is None
    assert summary["execution_arn"] == execution_arn_for(execution["name"])
    assert sns.messages[0]["subject"] == (
        "AFT weekly full run: re-applying every AFT-managed account"
    )


def execution_arn_for(name: str) -> str:
    return f"{STATE_MACHINE_ARN.replace(':stateMachine:', ':execution:')}:{name}"


def test_it_never_starts_a_pipeline_directly(sfn):
    """No CodePipeline client at all: AFT owns concurrency and completion."""
    run_all.lambda_handler(EVENT, None)

    assert not hasattr(run_all, "codepipeline")
    assert len(sfn.executions) == 1


def test_bypass_steps_is_always_sent(sfn):
    """Without it, the state machine runs the full provisioning framework."""
    run_all.lambda_handler(EVENT, None)

    assert only_execution(sfn)["input"]["bypass_steps"] == ["provisioning_bootstrap"]


def test_a_duplicate_delivery_does_not_re_apply_the_estate(sfn, sns):
    """Item 2: the EventBridge event id was discarded, so a redelivery re-ran everything.

    Carrying it into the execution name makes the second delivery a no-op.
    """
    first = run_all.lambda_handler(EVENT, None)
    second = run_all.lambda_handler(EVENT, None)

    assert len(sfn.executions) == 1
    assert first["already_invoked"] is False
    assert second["already_invoked"] is True
    assert second["execution_arn"] == first["execution_arn"]
    # Nothing happened the second time, so by default it is not reported.
    assert len(sns.messages) == 1


def test_a_suppressed_duplicate_is_published_when_notify_when_clean_is_set(monkeypatch, sns):
    monkeypatch.setenv("NOTIFY_WHEN_CLEAN", "true")

    run_all.lambda_handler(EVENT, None)
    summary = run_all.lambda_handler(EVENT, None)

    assert summary["already_invoked"] is True
    assert len(sns.messages) == 2
    assert sns.messages[1]["subject"] == "AFT weekly full run: already invoked, nothing to do"
    assert "duplicate delivery" in sns.messages[1]["message"]


def test_a_different_scheduled_event_is_a_different_execution(sfn):
    run_all.lambda_handler(EVENT, None)
    run_all.lambda_handler({"id": "a-later-week"}, None)

    assert len(sfn.executions) == 2


def test_dry_run_invokes_nothing(monkeypatch, sfn, sns):
    monkeypatch.setenv("DRY_RUN", "true")

    summary = run_all.lambda_handler(EVENT, None)

    assert sfn.executions == []
    assert summary["execution_arn"] is None
    assert summary["dry_run"] is True
    assert sns.messages[0]["subject"] == (
        "AFT weekly full run: would re-apply every AFT-managed account"
    )
    assert "DRY_RUN is set: AFT was not invoked." in sns.messages[0]["message"]


def test_an_invocation_failure_is_always_reported(monkeypatch, sfn, sns):
    """Nothing was re-applied, and there is no CodePipeline job to fail here."""
    monkeypatch.setenv("NOTIFY_WHEN_CLEAN", "false")
    sfn.fail = True

    summary = run_all.lambda_handler(EVENT, None)

    assert summary["invoke_error"] == "AccessDeniedException"
    assert summary["execution_arn"] is None
    assert sns.messages[0]["subject"] == (
        "AFT weekly full run: could not invoke aft-invoke-customizations"
    )
    assert "NOTHING was re-applied" in sns.messages[0]["message"]


def test_publish_failure_does_not_lose_the_run(sfn, sns):
    sns.fail = True

    summary = run_all.lambda_handler(EVENT, None)

    assert len(sfn.executions) == 1
    assert summary["invoke_error"] is None
    assert sns.messages == []


def test_a_manual_invocation_has_no_event_id_and_is_not_deduplicated(sfn):
    """A human invoking this twice means it twice."""
    first = run_all.lambda_handler({}, None)
    second = run_all.lambda_handler({}, None)

    assert len(sfn.executions) == 2
    assert "manual-" in first["execution_name"]
    assert first["execution_name"] != second["execution_name"]
