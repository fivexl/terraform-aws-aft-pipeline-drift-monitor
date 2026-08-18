"""Tests for the status report Lambda."""

from __future__ import annotations

import pytest

import status_report


@pytest.fixture(autouse=True)
def wire_clients(monkeypatch, cp, sns):
    monkeypatch.setattr(status_report, "codepipeline", cp)
    monkeypatch.setattr(status_report, "sns", sns)


def test_report_buckets_pipelines_by_outcome(cp, sns):
    report = status_report.lambda_handler({}, None)

    assert report["total"] == 4
    assert [s["pipeline"] for s in report["failed"]] == ["444444444444-customizations-pipeline"]
    assert [s["pipeline"] for s in report["running"]] == ["333333333333-customizations-pipeline"]
    # The running pipeline is excluded from "behind HEAD": it is being fixed already.
    assert [s["pipeline"] for s in report["drifted"]] == [
        "222222222222-customizations-pipeline",
        "444444444444-customizations-pipeline",
    ]

    message = sns.messages[0]
    assert message["subject"] == "AFT pipeline report: 1 failed, 2 behind HEAD"
    assert "Still running: 1" in message["message"]


def test_all_clear_subject(cp, sns):
    head = cp.pipelines["111111111111-customizations-pipeline"]
    for name in list(cp.pipelines):
        if name.endswith("-customizations-pipeline"):
            cp.pipelines[name] = head

    report = status_report.lambda_handler({}, None)

    assert report["failed"] == []
    assert report["drifted"] == []
    assert sns.messages[0]["subject"] == "AFT pipeline report: all 4 pipelines current"


def test_report_survives_a_probe_that_never_ran(cp, sns):
    cp.pipelines[status_report.os.environ["PROBE_PIPELINE_NAME"]] = []

    report = status_report.lambda_handler({}, None)

    assert report["head_revisions"] == {}
    # With no HEAD to compare against, nothing is reported as drifted.
    assert report["drifted"] == []
    assert "HEAD revisions: unavailable" in sns.messages[0]["message"]
