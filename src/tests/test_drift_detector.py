"""Tests for the drift detector Lambda."""

from __future__ import annotations

import pytest

import drift_detector
from tests.conftest import PROBE

DRIFTED = [
    "222222222222-customizations-pipeline",
    "444444444444-customizations-pipeline",
]


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


def test_starts_only_drifted_idle_pipelines(cp, sns):
    event = job_event(cp.pipelines[PROBE][0]["pipelineExecutionId"])

    result = drift_detector.lambda_handler(event, None)

    assert result["started"] == DRIFTED
    assert cp.started == DRIFTED
    # Already at HEAD, and already running, are both left alone.
    assert "111111111111-customizations-pipeline" not in cp.started
    assert result["skipped_already_running"] == ["333333333333-customizations-pipeline"]
    assert cp.job_results == [("job-1", "success")]
    assert len(sns.messages) == 1
    assert "Behind HEAD:       2" in sns.messages[0]["message"]


def test_dry_run_reports_without_starting_anything(monkeypatch, cp, sns):
    monkeypatch.setenv("DRY_RUN", "true")

    result = drift_detector.lambda_handler(job_event(cp.pipelines[PROBE][0]["pipelineExecutionId"]), None)

    assert result["drifted"] == DRIFTED
    assert result["started"] == []
    assert cp.started == []
    assert "Would start:       2" in sns.messages[0]["message"]


def test_respects_max_pipelines_per_run(monkeypatch, cp):
    monkeypatch.setenv("MAX_PIPELINES_PER_RUN", "1")

    result = drift_detector.lambda_handler(job_event(cp.pipelines[PROBE][0]["pipelineExecutionId"]), None)

    assert cp.started == DRIFTED[:1]
    assert result["deferred_over_limit"] == DRIFTED[1:]


def test_direct_invocation_resolves_head_from_latest_probe_execution(cp):
    result = drift_detector.lambda_handler({}, None)

    assert cp.started == DRIFTED
    assert cp.job_results == []
    assert result["pipelines_checked"] == 4


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
