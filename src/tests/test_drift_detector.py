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
    job_event,
    summary,
)

#: Behind HEAD, idle, and worth another run.
STARTED = [
    "222222222222-customizations-pipeline",
    "555555555555-customizations-pipeline",
]
RUNNING = "333333333333-customizations-pipeline"
FAILING_ON_HEAD = "444444444444-customizations-pipeline"
CURRENT = "111111111111-customizations-pipeline"


@pytest.fixture(autouse=True)
def wire_clients(monkeypatch, cp, sns):
    monkeypatch.setattr(drift_detector, "codepipeline", cp)
    monkeypatch.setattr(drift_detector, "sns", sns)


def test_head_comes_from_the_job_input_artifacts(cp):
    """AWS omits pipelineContext from Lambda action events, so the input
    artifacts' revisions are the only in-event source of this run's HEAD.

    The probe's history is poisoned with a stale commit: if it were consulted,
    HEAD would be wrong and the pipeline that is genuinely current would be
    restarted while the drifted ones were left alone.
    """
    cp.pipelines[PROBE] = [summary("Succeeded", OLD_GLOBAL, OLD_GLOBAL)]

    result = drift_detector.lambda_handler(job_event(), None)

    assert result["head_revisions"] == {GLOBAL: HEAD_GLOBAL, ACCOUNT: HEAD_ACCOUNT}
    assert PROBE not in cp.execution_lookups
    assert cp.started == STARTED
    assert CURRENT not in cp.started


def test_starts_only_drifted_idle_pipelines(cp, sns):
    result = drift_detector.lambda_handler(job_event(), None)

    assert cp.started == STARTED
    assert result["started"] == STARTED
    assert CURRENT not in cp.started
    assert result["skipped_already_running"] == [RUNNING]
    assert cp.job_results == [("job-1", "success")]
    assert len(sns.messages) == 1
    assert "2 behind HEAD" in sns.messages[0]["subject"]


def test_does_not_restart_a_pipeline_that_already_failed_on_head(cp, sns):
    """The restart loop guard: another run would only repeat the same failure."""
    result = drift_detector.lambda_handler(job_event(), None)

    assert FAILING_ON_HEAD not in cp.started
    assert result["failing_on_head"] == [FAILING_ON_HEAD]
    assert "failing on HEAD" in sns.messages[0]["subject"]
    assert "already ran HEAD and did not succeed" in sns.messages[0]["message"]


def test_dry_run_reports_without_starting_anything(monkeypatch, cp, sns):
    monkeypatch.setenv("DRY_RUN", "true")

    result = drift_detector.lambda_handler(job_event(), None)

    assert result["drifted"] == STARTED
    assert result["started"] == []
    assert cp.started == []
    assert "Would start:       2" in sns.messages[0]["message"]


def test_respects_max_pipelines_per_run(monkeypatch, cp):
    monkeypatch.setenv("MAX_PIPELINES_PER_RUN", "1")

    result = drift_detector.lambda_handler(job_event(), None)

    assert cp.started == STARTED[:1]
    assert result["deferred_over_limit"] == STARTED[1:]


def test_one_unstartable_pipeline_does_not_abort_the_others(cp, sns):
    cp.start_errors = {STARTED[0]}

    result = drift_detector.lambda_handler(job_event(), None)

    assert cp.started == STARTED[1:]
    assert result["failed_to_start"] == STARTED[:1]
    assert cp.job_results == [("job-1", "success")]
    assert "could not be started" in sns.messages[0]["subject"]


def test_renamed_source_action_fails_loudly(monkeypatch, cp, sns):
    """A silent rename would mark every pipeline drifted and start them all.

    Here the module's own SOURCE_ACTIONS is the side that no longer matches AFT.
    """
    monkeypatch.setenv("SOURCE_ACTIONS", "aft-global-customizations,aft-renamed")

    result = drift_detector.lambda_handler(job_event(), None)

    assert "source action" in result["error"]
    assert cp.started == []
    assert cp.job_results == [("job-1", "failure")]
    assert sns.messages == []


def test_guard_checks_one_real_aft_pipeline(cp):
    """The check has to read AFT's side, not the probe's mirror of our own config."""
    drift_detector.lambda_handler(job_event(), None)

    assert cp.described == [CURRENT]


def test_source_action_renamed_on_the_aft_side_fails_loudly(cp, sns):
    """AFT renames a source action: the probe cannot see it, GetPipeline can."""
    cp.source_action_names[CURRENT] = [
        "aft-global-customizations",
        "aft-account-customizations-v2",
    ]

    result = drift_detector.lambda_handler(job_event(), None)

    assert f"AFT pipeline {CURRENT} is configured with source actions" in result["error"]
    assert "aft-account-customizations-v2" in result["error"]
    assert cp.started == []
    assert cp.job_results == [("job-1", "failure")]
    assert sns.messages == []


def test_source_action_added_on_the_aft_side_fails_loudly(cp):
    cp.source_action_names[CURRENT] = [
        "aft-global-customizations",
        "aft-account-customizations",
        "aft-account-provisioning-customizations",
    ]

    result = drift_detector.lambda_handler(job_event(), None)

    assert "aft-account-provisioning-customizations" in result["error"]
    assert cp.started == []


def test_guard_is_skipped_when_no_aft_pipelines_exist(cp):
    """Nothing to compare against, and nothing to start either."""
    for name in [n for n in cp.pipelines if n.endswith("-customizations-pipeline")]:
        del cp.pipelines[name]

    result = drift_detector.lambda_handler(job_event(), None)

    assert cp.described == []
    assert result["pipelines_checked"] == 0
    assert cp.job_results == [("job-1", "success")]


def test_publish_failure_does_not_fail_the_job(cp, sns):
    """Starts already succeeded, so a notification error must not invert the result."""
    sns.fail = True

    result = drift_detector.lambda_handler(job_event(), None)

    assert cp.started == STARTED
    assert result["started"] == STARTED
    assert cp.job_results == [("job-1", "success")]


def test_unreportable_job_result_falls_back_to_sns(cp, sns):
    cp.pipelines[PROBE] = []
    cp.job_result_errors = True

    result = drift_detector.lambda_handler({"CodePipeline.job": {"id": "job-2", "data": {}}}, None)

    assert "Could not resolve HEAD" in result["error"]
    assert cp.job_results == []
    assert sns.messages[0]["subject"] == "AFT pipeline drift check failed"


def test_unreported_job_success_alerts_without_changing_the_result(cp, sns):
    """A dropped success callback leaves the action hanging, so it must be alerted."""
    cp.job_result_errors = True

    result = drift_detector.lambda_handler(job_event(), None)

    # The work stands: pipelines were started and the summary is returned as usual.
    assert cp.started == STARTED
    assert result["started"] == STARTED
    assert cp.job_results == []

    alert = sns.messages[-1]
    assert alert["subject"] == "AFT drift check succeeded but CodePipeline was not notified"
    assert "job-1" in alert["message"]
    assert "action timeout" in alert["message"]


def test_unreported_job_success_alert_failure_does_not_break_the_result(cp, sns):
    """The alert is a best-effort side channel; SNS being down must not raise."""
    cp.job_result_errors = True
    sns.fail = True

    result = drift_detector.lambda_handler(job_event(), None)

    assert result["started"] == STARTED
    assert sns.messages == []


def test_direct_invocation_resolves_head_from_latest_probe_execution(cp):
    """No job means no input artifacts, so the probe's history is the fallback."""
    result = drift_detector.lambda_handler({}, None)

    assert PROBE in cp.execution_lookups
    assert cp.started == STARTED
    assert cp.job_results == []
    assert result["pipelines_checked"] == 5


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
