"""Test fixtures: a fake CodePipeline client and a fake SNS client.

No moto, no fixtures directory - just enough of the API surface that the
handlers exercise, so a logic regression fails loudly.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("AWS_DEFAULT_REGION", "eu-central-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

GLOBAL = "aft-global-customizations"
ACCOUNT = "aft-account-customizations"
PROBE = "aft-pipeline-drift-monitor-revision-probe"
HEAD_GLOBAL = "1111111111111111111111111111111111111111"
HEAD_ACCOUNT = "2222222222222222222222222222222222222222"
OLD_GLOBAL = "9999999999999999999999999999999999999999"


def summary(status: str, global_rev: str, account_rev: str, minutes_ago: int = 0) -> dict:
    """Build a pipelineExecutionSummary as CodePipeline returns it.

    ``minutes_ago`` really moves ``startTime``, so ordering by recency is testable.
    """
    return {
        "pipelineExecutionId": f"exec-{status}-{minutes_ago}",
        "status": status,
        "startTime": datetime(2026, 8, 18, 12, 0, tzinfo=UTC) - timedelta(minutes=minutes_ago),
        "sourceRevisions": [
            {"actionName": GLOBAL, "revisionId": global_rev},
            {"actionName": ACCOUNT, "revisionId": account_rev},
        ],
    }


class FakeCodePipeline:
    """In-memory stand-in for the CodePipeline client."""

    def __init__(self, pipelines: dict[str, list[dict]]):
        self.pipelines = pipelines
        self.started: list[str] = []
        self.job_results: list[tuple[str, str]] = []
        #: Set to mimic an in-progress execution that has not recorded its
        #: artifact revisions yet.
        self.hide_artifact_revisions = False
        #: Pipeline names whose start_pipeline_execution should raise.
        self.start_errors: set[str] = set()
        #: Set to make the job-result calls raise.
        self.job_result_errors = False

    # -- discovery ---------------------------------------------------------
    def get_paginator(self, operation: str):
        assert operation == "list_pipelines"
        pages = [{"pipelines": [{"name": name} for name in self.pipelines]}]

        class _Paginator:
            def paginate(self):
                return iter(pages)

        return _Paginator()

    # -- executions --------------------------------------------------------
    def list_pipeline_executions(self, pipelineName: str, maxResults: int = 10):  # noqa: N803
        return {"pipelineExecutionSummaries": self.pipelines.get(pipelineName, [])[:maxResults]}

    def get_pipeline_execution(self, pipelineName: str, pipelineExecutionId: str):  # noqa: N803
        for item in self.pipelines.get(pipelineName, []):
            if item["pipelineExecutionId"] == pipelineExecutionId:
                if self.hide_artifact_revisions:
                    return {"pipelineExecution": {}}
                return {
                    "pipelineExecution": {
                        "artifactRevisions": [
                            # CodePipeline reports artifact names here, not action names.
                            {"name": f"source-{rev['actionName']}", "revisionId": rev["revisionId"]}
                            for rev in item["sourceRevisions"]
                        ]
                    }
                }
        raise AssertionError(f"unknown execution {pipelineExecutionId}")

    def start_pipeline_execution(self, name: str):
        if name in self.start_errors:
            raise RuntimeError(f"PipelineNotFoundException: {name}")
        self.started.append(name)
        return {"pipelineExecutionId": f"new-exec-{name}"}

    # -- job protocol ------------------------------------------------------
    def put_job_success_result(self, jobId: str):  # noqa: N803
        if self.job_result_errors:
            raise RuntimeError("InvalidJobStateException")
        self.job_results.append((jobId, "success"))

    def put_job_failure_result(self, jobId: str, failureDetails: dict):  # noqa: N803
        if self.job_result_errors:
            raise RuntimeError("InvalidJobStateException")
        # The API rejects an empty message, so record it and let tests assert on it.
        assert failureDetails["message"], "failureDetails.message must be 1-5000 characters"
        self.job_results.append((jobId, "failure"))


class FakeSns:
    """Records published messages."""

    def __init__(self):
        self.messages: list[dict] = []
        #: Set to make publish raise, as a KMS denial or throttle would.
        self.fail = False

    def publish(self, TopicArn: str, Subject: str, Message: str):  # noqa: N803
        if self.fail:
            raise RuntimeError("KMSAccessDeniedException")
        self.messages.append({"topic": TopicArn, "subject": Subject, "message": Message})


@pytest.fixture
def pipelines() -> dict[str, list[dict]]:
    """One pipeline per case the drift detector has to distinguish.

    * ``111...`` current at HEAD - left alone
    * ``222...`` last success on an older commit - started
    * ``333...`` stale but an execution is in flight - skipped
    * ``444...`` newest execution already ran HEAD and failed - reported, not restarted
    * ``555...`` failed on an older commit, never succeeded - started
    """
    return {
        PROBE: [summary("Succeeded", HEAD_GLOBAL, HEAD_ACCOUNT)],
        "111111111111-customizations-pipeline": [
            summary("Succeeded", HEAD_GLOBAL, HEAD_ACCOUNT, minutes_ago=10)
        ],
        "222222222222-customizations-pipeline": [
            summary("Succeeded", OLD_GLOBAL, HEAD_ACCOUNT, minutes_ago=30)
        ],
        "333333333333-customizations-pipeline": [
            summary("InProgress", HEAD_GLOBAL, HEAD_ACCOUNT, minutes_ago=1),
            summary("Succeeded", OLD_GLOBAL, HEAD_ACCOUNT, minutes_ago=60),
        ],
        "444444444444-customizations-pipeline": [
            summary("Failed", HEAD_GLOBAL, HEAD_ACCOUNT, minutes_ago=20)
        ],
        "555555555555-customizations-pipeline": [
            summary("Failed", OLD_GLOBAL, HEAD_ACCOUNT, minutes_ago=40)
        ],
        "aft-account-request": [summary("Succeeded", HEAD_GLOBAL, HEAD_ACCOUNT)],
    }


@pytest.fixture
def cp(pipelines) -> FakeCodePipeline:
    return FakeCodePipeline(pipelines)


@pytest.fixture
def sns() -> FakeSns:
    return FakeSns()


@pytest.fixture(autouse=True)
def base_env(monkeypatch):
    monkeypatch.setenv("PROBE_PIPELINE_NAME", PROBE)
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:eu-central-1:123456789012:aft-drift")
    monkeypatch.setenv("SOURCE_ACTIONS", f"{GLOBAL},{ACCOUNT}")
    monkeypatch.setenv("MAX_PIPELINES_PER_RUN", "20")
    monkeypatch.delenv("DRY_RUN", raising=False)
