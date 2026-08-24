"""Tests for the drift detector Lambda."""

from __future__ import annotations

import pytest

import drift_detector
from tests.conftest import PROBE

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


def job_event(execution_id: str) -> dict:
    return {
        "CodePipeline.job": {
            "id": "job-1",
            "data": {
                "pipelineContext": {
                    "pipelineName": PROBE,
                    "pipelineExecutionId": execution_id,
                }
            },
        }
    }


def probe_job(cp) -> dict:
    return job_event(cp.pipelines[PROBE][0]["pipelineExecutionId"])


def test_starts_only_drifted_idle_pipelines(cp, sns):
    result = drift_detector.lambda_handler(probe_job(cp), None)

    assert cp.started == STARTED
    assert result["started"] == STARTED
    assert CURRENT not in cp.started
    assert result["skipped_already_running"] == [RUNNING]
    assert cp.job_results == [("job-1", "success")]
    assert len(sns.messages) == 1
    assert "2 behind HEAD" in sns.messages[0]["subject"]


def test_does_not_restart_a_pipeline_that_already_failed_on_head(cp, sns):
    """The restart loop guard: another run would only repeat the same failure."""
    result = drift_detector.lambda_handler(probe_job(cp), None)

    assert FAILING_ON_HEAD not in cp.started
    assert result["failing_on_head"] == [FAILING_ON_HEAD]
    assert "failing on HEAD" in sns.messages[0]["subject"]
    assert "already ran HEAD and did not succeed" in sns.messages[0]["message"]


def test_dry_run_reports_without_starting_anything(monkeypatch, cp, sns):
    monkeypatch.setenv("DRY_RUN", "true")

    result = drift_detector.lambda_handler(probe_job(cp), None)

    assert result["drifted"] == STARTED
    assert result["started"] == []
    assert cp.started == []
    assert "Would start:       2" in sns.messages[0]["message"]


def test_respects_max_pipelines_per_run(monkeypatch, cp):
    monkeypatch.setenv("MAX_PIPELINES_PER_RUN", "1")

    result = drift_detector.lambda_handler(probe_job(cp), None)

    assert cp.started == STARTED[:1]
    assert result["deferred_over_limit"] == STARTED[1:]


def test_one_unstartable_pipeline_does_not_abort_the_others(cp, sns):
    cp.start_errors = {STARTED[0]}

    result = drift_detector.lambda_handler(probe_job(cp), None)

    assert cp.started == STARTED[1:]
    assert result["failed_to_start"] == STARTED[:1]
    assert cp.job_results == [("job-1", "success")]
    assert "could not be started" in sns.messages[0]["subject"]


def test_renamed_source_action_fails_loudly(monkeypatch, cp, sns):
    """A silent rename would mark every pipeline drifted and start them all."""
    monkeypatch.setenv("SOURCE_ACTIONS", "aft-global-customizations,aft-renamed")

    result = drift_detector.lambda_handler(probe_job(cp), None)

    assert "source action" in result["error"]
    assert cp.started == []
    assert cp.job_results == [("job-1", "failure")]
    assert sns.messages == []


def test_publish_failure_does_not_fail_the_job(cp, sns):
    """Starts already succeeded, so a notification error must not invert the result."""
    sns.fail = True

    result = drift_detector.lambda_handler(probe_job(cp), None)

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

    result = drift_detector.lambda_handler(probe_job(cp), None)

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

    result = drift_detector.lambda_handler(probe_job(cp), None)

    assert result["started"] == STARTED
    assert sns.messages == []


def test_direct_invocation_resolves_head_from_latest_probe_execution(cp):
    result = drift_detector.lambda_handler({}, None)

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
