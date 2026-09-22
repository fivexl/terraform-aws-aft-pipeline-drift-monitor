"""Tests for the drift detector Lambda."""

from __future__ import annotations

import pytest

import drift_detector
from tests.conftest import (
    ACCOUNT,
    GLOBAL,
    HEAD_ACCOUNT,
    HEAD_GLOBAL,
    OLD_GLOBAL,
    PROBE,
    STATE_MACHINE_ARN,
    job_event,
    summary,
)

#: Behind HEAD, idle, and worth another run.
STARTED = [
    "222222222222-customizations-pipeline",
    "555555555555-customizations-pipeline",
]
#: The accounts those pipelines belong to - what the state machine is given.
ACCOUNTS = ["222222222222", "555555555555"]
RUNNING = "333333333333-customizations-pipeline"
FAILING_ON_HEAD = "444444444444-customizations-pipeline"
CURRENT = "111111111111-customizations-pipeline"


@pytest.fixture(autouse=True)
def wire_clients(monkeypatch, cp, sfn, sns):
    monkeypatch.setattr(drift_detector, "codepipeline", cp)
    monkeypatch.setattr(drift_detector, "stepfunctions", sfn)
    monkeypatch.setattr(drift_detector, "sns", sns)


def only_execution(sfn) -> dict:
    assert len(sfn.executions) == 1, sfn.executions
    return sfn.executions[0]


def test_drifted_accounts_go_to_aft_in_one_execution(cp, sfn, sns):
    """The whole point: AFT is handed the accounts and owns the concurrency."""
    result = drift_detector.lambda_handler(job_event(), None)

    execution = only_execution(sfn)
    assert execution["stateMachineArn"] == STATE_MACHINE_ARN
    # include is capped at 4 items by AFT's schema, so every account travels in
    # ONE accounts selector, never one selector each.
    assert execution["input"] == {
        "include": [{"type": "accounts", "target_value": ACCOUNTS}],
        "bypass_steps": ["provisioning_bootstrap"],
    }
    assert result["invoked_accounts"] == ACCOUNTS
    assert result["execution_arn"] == execution_arn_for(execution["name"])
    assert cp.job_results == [("job-1", "success")]
    assert "2 behind HEAD" in sns.messages[0]["subject"]


def execution_arn_for(name: str) -> str:
    return f"{STATE_MACHINE_ARN.replace(':stateMachine:', ':execution:')}:{name}"


def test_the_state_machine_input_never_carries_get_execution_id(sfn):
    """The state machine's first state injects it from $$.Execution.Name.

    Supplying one would be overwritten, so sending it would be misleading.
    """
    drift_detector.lambda_handler(job_event(), None)

    assert "get_execution_id" not in only_execution(sfn)["input"]


def test_bypass_steps_is_always_sent(sfn):
    """Without it, Check Bypass falls through to the full provisioning framework.

    The choice reads `'provisioning_bootstrap' in $states.input.bypass_steps` in
    JSONata, so an absent key is simply false - silently the heavy path.
    """
    drift_detector.lambda_handler(job_event(), None)

    assert only_execution(sfn)["input"]["bypass_steps"] == ["provisioning_bootstrap"]


def test_a_duplicate_delivery_does_not_re_run_anything(cp, sfn):
    """Item 2: Lambda can deliver the same event twice, including after success.

    The execution name is derived from the CodePipeline job id, so the second
    delivery hits ExecutionAlreadyExists and starts nothing.
    """
    first = drift_detector.lambda_handler(job_event(), None)
    second = drift_detector.lambda_handler(job_event(), None)

    assert len(sfn.executions) == 1
    assert first["already_invoked"] is False
    assert second["already_invoked"] is True
    # The ARN is still reported, so an operator can follow the original run.
    assert second["execution_arn"] == first["execution_arn"]
    # A suppressed duplicate is not a failure.
    assert cp.job_results == [("job-1", "success"), ("job-1", "success")]


def test_a_different_account_set_is_not_suppressed(cp, sfn):
    """A retry that resolved different accounts is new work, not a duplicate."""
    drift_detector.lambda_handler(job_event(), None)
    # 111... falls behind too, so the second delivery targets three accounts.
    cp.pipelines[CURRENT] = [summary("Succeeded", OLD_GLOBAL, HEAD_ACCOUNT, minutes_ago=10)]

    result = drift_detector.lambda_handler(job_event(), None)

    assert len(sfn.executions) == 2
    assert result["already_invoked"] is False
    assert result["invoked_accounts"] == ["111111111111", *ACCOUNTS]


def test_head_comes_from_the_job_input_artifacts(cp, sfn):
    """AWS omits pipelineContext from Lambda action events, so the input
    artifacts' revisions are the only in-event source of this run's HEAD.

    The probe's history is poisoned with a stale commit: if it were consulted,
    HEAD would be wrong and the account that is genuinely current would be
    re-run while the drifted ones were left alone.
    """
    cp.pipelines[PROBE] = [summary("Succeeded", OLD_GLOBAL, OLD_GLOBAL)]

    result = drift_detector.lambda_handler(job_event(), None)

    assert result["head_revisions"] == {GLOBAL: HEAD_GLOBAL, ACCOUNT: HEAD_ACCOUNT}
    assert PROBE not in cp.execution_lookups
    assert only_execution(sfn)["input"]["include"][0]["target_value"] == ACCOUNTS


def test_does_not_re_run_a_pipeline_that_already_failed_on_head(sns):
    """The restart loop guard: another run would only repeat the same failure."""
    result = drift_detector.lambda_handler(job_event(), None)

    assert "444444444444" not in result["invoked_accounts"]
    assert result["failing_on_head"] == [FAILING_ON_HEAD]
    assert "failing on HEAD" in sns.messages[0]["subject"]
    assert "already ran HEAD and failed" in sns.messages[0]["message"]


def test_a_current_pipeline_is_left_alone(sfn):
    drift_detector.lambda_handler(job_event(), None)

    assert "111111111111" not in only_execution(sfn)["input"]["include"][0]["target_value"]


def test_a_running_pipeline_is_skipped(sfn):
    result = drift_detector.lambda_handler(job_event(), None)

    assert result["skipped_already_running"] == [RUNNING]
    assert "333333333333" not in only_execution(sfn)["input"]["include"][0]["target_value"]


def test_dry_run_reports_without_invoking_aft(monkeypatch, sfn, sns):
    monkeypatch.setenv("DRY_RUN", "true")

    result = drift_detector.lambda_handler(job_event(), None)

    assert sfn.executions == []
    assert result["execution_arn"] is None
    assert result["invoked_accounts"] == ACCOUNTS
    assert "Would re-run" in sns.messages[0]["message"]
    assert "DRY_RUN is set: AFT was not invoked." in sns.messages[0]["message"]


def test_an_invocation_failure_fails_the_job_and_is_reported(cp, sfn, sns):
    """Nothing is re-run, so this must never be reported as a successful check."""
    sfn.fail = True

    result = drift_detector.lambda_handler(job_event(), None)

    assert result["invoke_error"] == "AccessDeniedException"
    assert result["execution_arn"] is None
    assert cp.job_results == [("job-1", "failure")]
    assert "could not invoke AFT" in sns.messages[0]["subject"]
    assert "NOTHING was re-run" in sns.messages[0]["message"]


def test_an_invocation_failure_is_reported_even_when_notifications_are_off(monkeypatch, cp, sfn, sns):
    """notify_on_drift is informational and must not hide an operational failure."""
    monkeypatch.setenv("NOTIFY_ON_DRIFT", "false")
    sfn.fail = True

    drift_detector.lambda_handler(job_event(), None)

    assert cp.job_results == [("job-1", "failure")]
    assert "could not invoke AFT" in sns.messages[0]["subject"]


def test_a_clean_run_is_silent_when_notifications_are_off(monkeypatch, cp, sfn, sns):
    """The flag still suppresses the purely informational drift summary."""
    monkeypatch.setenv("NOTIFY_ON_DRIFT", "false")

    result = drift_detector.lambda_handler(job_event(), None)

    assert result["invoked_accounts"] == ACCOUNTS
    assert len(sfn.executions) == 1
    assert cp.job_results == [("job-1", "success")]
    assert sns.messages == []


def test_a_pipeline_with_no_account_id_is_reported_not_dropped(monkeypatch, cp, sns):
    """The state machine selects accounts, so a nameless pipeline is unrunnable.

    Only reachable by overriding pipeline_name_pattern with a shape that does not
    pin the account id - but silently skipping it would be a false clean.
    """
    monkeypatch.setenv("PIPELINE_NAME_PATTERN", r".*-customizations-pipeline$")
    cp.pipelines["team-alpha-customizations-pipeline"] = [
        summary("Succeeded", OLD_GLOBAL, HEAD_ACCOUNT, minutes_ago=50)
    ]

    result = drift_detector.lambda_handler(job_event(), None)

    assert result["unresolved_accounts"] == ["team-alpha-customizations-pipeline"]
    assert result["invoked_accounts"] == ACCOUNTS
    assert cp.job_results == [("job-1", "failure")]
    assert "1 without an account id" in sns.messages[0]["subject"]
    assert "No account id in the pipeline name" in sns.messages[0]["message"]


def test_source_actions_that_do_not_match_head_fail_loudly(monkeypatch, cp, sfn, sns):
    """The module tracking an action the probe never resolved is a partial HEAD.

    Judging drift on it would treat every account as current on the action that
    is missing, so the run must fail rather than report a false all-clear.
    """
    monkeypatch.setenv("SOURCE_ACTIONS", "aft-global-customizations,aft-renamed")

    result = drift_detector.lambda_handler(job_event(), None)

    assert "partial HEAD" in result["error"]
    assert "aft-renamed" in result["error"]
    assert sfn.executions == []
    assert cp.job_results == [("job-1", "failure")]
    assert sns.messages == []


def test_a_partial_head_from_the_probe_fails_the_job(cp, sfn):
    """The probe resolved only one of the two sources - reproduces item 3."""
    result = drift_detector.lambda_handler(
        {
            "CodePipeline.job": {
                "id": "job-1",
                "data": {"inputArtifacts": [{"name": GLOBAL, "revision": HEAD_GLOBAL}]},
            }
        },
        None,
    )

    assert "partial HEAD" in result["error"]
    assert sfn.executions == []
    assert cp.job_results == [("job-1", "failure")]


def test_guard_checks_every_aft_pipeline(cp):
    """One hand-edited pipeline must neither block the estate nor slip through.

    Sampling pipelines[0] did both, depending on which pipeline was the outlier.
    """
    drift_detector.lambda_handler(job_event(), None)

    assert sorted(cp.described) == sorted(
        [name for name in cp.pipelines if name.endswith("-customizations-pipeline")]
    )


def test_an_incompatible_pipeline_is_quarantined_and_the_rest_still_run(cp, sfn, sns):
    """AFT's side renamed on ONE pipeline: it is quarantined, others proceed."""
    cp.source_action_names[STARTED[0]] = [GLOBAL, "aft-account-customizations-v2"]

    result = drift_detector.lambda_handler(job_event(), None)

    assert STARTED[0] in result["quarantined"]
    assert "aft-account-customizations-v2" in result["quarantined"][STARTED[0]]
    # The quarantined pipeline is neither compared nor re-run ...
    assert result["invoked_accounts"] == ACCOUNTS[1:]
    assert STARTED[0] not in result["drifted"]
    # ... while every compatible pipeline is still remediated.
    assert only_execution(sfn)["input"]["include"][0]["target_value"] == ACCOUNTS[1:]
    # A quarantined pipeline is unremediated drift, so the job must fail.
    assert cp.job_results == [("job-1", "failure")]
    assert "1 quarantined" in sns.messages[0]["subject"]
    assert "Quarantined" in sns.messages[0]["message"]


def test_an_unreadable_pipeline_definition_is_quarantined_not_assumed_compatible(cp):
    """An unreadable definition is not evidence the source actions still match."""
    cp.describe_errors = {STARTED[0]}

    result = drift_detector.lambda_handler(job_event(), None)

    assert "could not read its definition" in result["quarantined"][STARTED[0]]
    assert result["invoked_accounts"] == ACCOUNTS[1:]
    assert cp.job_results == [("job-1", "failure")]


def test_one_uninspectable_pipeline_does_not_abort_the_estate(cp, sns):
    """A deletion race on one pipeline used to cost every account its run."""
    cp.inspect_errors = {STARTED[0]}

    result = drift_detector.lambda_handler(job_event(), None)

    assert [e["pipeline"] for e in result["inspect_errors"]] == [STARTED[0]]
    assert result["invoked_accounts"] == ACCOUNTS[1:]
    assert result["pipelines_checked"] == 4
    assert cp.job_results == [("job-1", "failure")]
    assert "1 could not be inspected" in sns.messages[0]["subject"]


def test_a_skipped_running_pipeline_is_notified(cp, sfn, sns):
    """notify_on_drift promises a summary for skipped pipelines - item 10.

    Only the already-running pipeline drifts here, so the old gate (drifted or
    failing or failed_to_start) published nothing at all.
    """
    for name in [CURRENT, STARTED[0], STARTED[1], FAILING_ON_HEAD]:
        cp.pipelines[name] = [summary("Succeeded", HEAD_GLOBAL, HEAD_ACCOUNT, minutes_ago=5)]

    result = drift_detector.lambda_handler(job_event(), None)

    assert result["skipped_already_running"] == [RUNNING]
    assert result["drifted"] == []
    # Nothing to hand over, so AFT is not invoked at all.
    assert sfn.executions == []
    assert len(sns.messages) == 1
    assert "1 already running" in sns.messages[0]["subject"]
    assert "Skipped, already running" in sns.messages[0]["message"]
    assert cp.job_results == [("job-1", "success")]


def test_nothing_drifted_invokes_nothing(cp, sfn, sns):
    for name in list(cp.pipelines):
        if name.endswith("-customizations-pipeline"):
            cp.pipelines[name] = [summary("Succeeded", HEAD_GLOBAL, HEAD_ACCOUNT, minutes_ago=5)]

    result = drift_detector.lambda_handler(job_event(), None)

    assert result["drifted"] == []
    assert sfn.executions == []
    assert sns.messages == []
    assert cp.job_results == [("job-1", "success")]


def test_guard_is_skipped_when_no_aft_pipelines_exist(cp, sfn):
    """Nothing to compare against, and nothing to re-run either."""
    for name in [n for n in cp.pipelines if n.endswith("-customizations-pipeline")]:
        del cp.pipelines[name]

    result = drift_detector.lambda_handler(job_event(), None)

    assert cp.described == []
    assert result["pipelines_checked"] == 0
    assert sfn.executions == []
    assert cp.job_results == [("job-1", "success")]


def test_publish_failure_does_not_fail_the_job(cp, sfn, sns):
    """AFT was invoked, so a notification error must not invert the result."""
    sns.fail = True

    result = drift_detector.lambda_handler(job_event(), None)

    assert len(sfn.executions) == 1
    assert result["invoked_accounts"] == ACCOUNTS
    assert cp.job_results == [("job-1", "success")]


def test_unreportable_job_result_falls_back_to_sns(cp, sns):
    cp.pipelines[PROBE] = []
    cp.job_result_errors = True

    result = drift_detector.lambda_handler({"CodePipeline.job": {"id": "job-2", "data": {}}}, None)

    assert "Could not resolve HEAD" in result["error"]
    assert cp.job_results == []
    assert sns.messages[0]["subject"] == "AFT pipeline drift check failed"


def test_unreported_job_success_alerts_without_changing_the_result(cp, sfn, sns):
    """A dropped success callback leaves the action hanging, so it must be alerted."""
    cp.job_result_errors = True

    result = drift_detector.lambda_handler(job_event(), None)

    # The work stands: AFT was invoked and the summary is returned as usual.
    assert len(sfn.executions) == 1
    assert result["invoked_accounts"] == ACCOUNTS
    assert cp.job_results == []

    alert = sns.messages[-1]
    assert alert["subject"] == "AFT drift check succeeded but CodePipeline was not notified"
    assert "job-1" in alert["message"]
    assert "action timeout" in alert["message"]


def test_unreported_degraded_result_says_so(cp, sfn, sns):
    cp.job_result_errors = True
    sfn.fail = True

    drift_detector.lambda_handler(job_event(), None)

    alert = sns.messages[-1]
    assert alert["subject"] == (
        "AFT drift check completed with failures but CodePipeline was not notified"
    )
    assert "could not be invoked" in alert["message"]


def test_unreported_job_success_alert_failure_does_not_break_the_result(cp, sns):
    """The alert is a best-effort side channel; SNS being down must not raise."""
    cp.job_result_errors = True
    sns.fail = True

    result = drift_detector.lambda_handler(job_event(), None)

    assert result["invoked_accounts"] == ACCOUNTS
    assert sns.messages == []


def test_direct_invocation_resolves_head_from_latest_probe_execution(cp, sfn):
    """No job means no input artifacts, so the probe's history is the fallback."""
    result = drift_detector.lambda_handler({}, None)

    assert PROBE in cp.execution_lookups
    assert result["invoked_accounts"] == ACCOUNTS
    assert cp.job_results == []
    assert result["pipelines_checked"] == 5
    # No stable id to key on, so a manual run is deliberately not idempotent.
    assert "manual-" in only_execution(sfn)["name"]


def test_two_direct_invocations_are_not_suppressed(sfn):
    """A human invoking this twice means it twice; there is no event id to dedupe."""
    drift_detector.lambda_handler({}, None)
    drift_detector.lambda_handler({}, None)

    assert len(sfn.executions) == 2


def test_unresolvable_head_fails_the_pipeline_job(cp, sns):
    cp.pipelines[PROBE] = []

    result = drift_detector.lambda_handler({"CodePipeline.job": {"id": "job-2", "data": {}}}, None)

    assert "Could not resolve HEAD" in result["error"]
    assert cp.job_results == [("job-2", "failure")]
    # The EventBridge failure rule alerts on the failed pipeline, so no duplicate SNS.
    assert sns.messages == []


def test_unresolvable_head_alerts_when_invoked_directly(cp, sns):
    cp.pipelines[PROBE] = []

    with pytest.raises(RuntimeError):
        drift_detector.lambda_handler({}, None)

    assert sns.messages[0]["subject"] == "AFT pipeline drift check failed"


def test_a_failure_with_no_message_still_answers_the_job(cp):
    """An exception whose str() is empty must not leave the job unanswered."""
    cp.pipelines[PROBE] = [{"pipelineExecutionId": "x", "status": "Succeeded", "sourceRevisions": []}]

    result = drift_detector.lambda_handler({"CodePipeline.job": {"id": "job-3", "data": {}}}, None)

    assert cp.job_results == [("job-3", "failure")]
    assert result["error"]
