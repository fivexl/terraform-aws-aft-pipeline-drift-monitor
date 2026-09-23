"""Tests for the status report Lambda."""

from __future__ import annotations

import pytest

import status_report
from tests.conftest import GLOBAL, HEAD_GLOBAL, PROBE


@pytest.fixture(autouse=True)
def wire_clients(monkeypatch, cp, sns):
    monkeypatch.setattr(status_report, "codepipeline", cp)
    monkeypatch.setattr(status_report, "sns", sns)


def test_report_buckets_pipelines_by_outcome(sns):
    report = status_report.lambda_handler({}, None)

    assert report["total"] == 5
    assert [s["pipeline"] for s in report["failed"]] == [
        "444444444444-customizations-pipeline",
        "555555555555-customizations-pipeline",
    ]
    assert [s["pipeline"] for s in report["running"]] == ["333333333333-customizations-pipeline"]
    # The running pipeline is excluded from "behind HEAD": it is being fixed already.
    assert [s["pipeline"] for s in report["drifted"]] == [
        "222222222222-customizations-pipeline",
        "444444444444-customizations-pipeline",
        "555555555555-customizations-pipeline",
    ]

    message = sns.messages[0]
    assert message["subject"] == "AFT pipeline report: 2 failed, 3 behind HEAD"
    assert "Still running: 1" in message["message"]


def test_all_clear_is_not_published_by_default(cp, sns):
    head = cp.pipelines["111111111111-customizations-pipeline"]
    for name in list(cp.pipelines):
        if name.endswith("-customizations-pipeline"):
            cp.pipelines[name] = head

    report = status_report.lambda_handler({}, None)

    assert report["failed"] == []
    assert report["drifted"] == []
    assert report["head_complete"] is True
    # Nothing to act on, and NOTIFY_WHEN_CLEAN is unset: no SNS publish.
    assert sns.messages == []


def test_all_clear_is_published_when_notify_when_clean_is_set(monkeypatch, cp, sns):
    monkeypatch.setenv("NOTIFY_WHEN_CLEAN", "true")
    head = cp.pipelines["111111111111-customizations-pipeline"]
    for name in list(cp.pipelines):
        if name.endswith("-customizations-pipeline"):
            cp.pipelines[name] = head

    report = status_report.lambda_handler({}, None)

    assert report["failed"] == []
    assert report["drifted"] == []
    assert sns.messages[0]["subject"] == "AFT pipeline report: all 5 pipelines current"


def test_report_survives_a_probe_that_never_ran(cp, sns):
    cp.pipelines[status_report.os.environ["PROBE_PIPELINE_NAME"]] = []

    report = status_report.lambda_handler({}, None)

    assert report["head_revisions"] == {}
    assert report["head_complete"] is False
    # With no HEAD to compare against, nothing is reported as drifted - so the
    # subject must not claim everything is current.
    assert report["drifted"] == []
    assert sns.messages[0]["subject"] == "AFT pipeline report: HEAD unavailable, 2 failed"
    assert "HEAD revisions: unavailable" in sns.messages[0]["message"]


def test_a_partial_head_is_not_treated_as_ground_truth(cp, sns):
    # The probe execution resolved the global source but not the account one yet.
    probe = cp.pipelines[PROBE][0]
    probe["sourceRevisions"] = [r for r in probe["sourceRevisions"] if r["actionName"] == GLOBAL]

    report = status_report.lambda_handler({}, None)

    assert report["head_revisions"] == {GLOBAL: HEAD_GLOBAL}
    assert report["head_complete"] is False
    assert sns.messages[0]["subject"] == "AFT pipeline report: HEAD incomplete, 2 failed"
    assert "HEAD revisions: incomplete" in sns.messages[0]["message"]


def test_a_partial_head_never_reports_everything_current(cp, sns):
    # Every pipeline sits at HEAD on the one action the probe did resolve, which
    # is exactly the shape that used to produce a false "all N current".
    head = cp.pipelines["111111111111-customizations-pipeline"]
    for name in list(cp.pipelines):
        if name.endswith("-customizations-pipeline"):
            cp.pipelines[name] = head
    probe = cp.pipelines[PROBE][0]
    probe["sourceRevisions"] = [r for r in probe["sourceRevisions"] if r["actionName"] == GLOBAL]

    report = status_report.lambda_handler({}, None)

    assert report["drifted"] == []
    assert "current" not in sns.messages[0]["subject"]
    assert sns.messages[0]["subject"] == "AFT pipeline report: HEAD incomplete, 0 failed"


def test_no_matching_pipelines_is_not_reported_as_all_current(cp, sns):
    for name in list(cp.pipelines):
        if name.endswith("-customizations-pipeline"):
            del cp.pipelines[name]

    report = status_report.lambda_handler({}, None)

    assert report["total"] == 0
    assert sns.messages[0]["subject"] == (
        "AFT pipeline report: no pipelines found matching ^\\d{12}-customizations-pipeline$"
    )


def test_a_failed_pipeline_is_always_published_regardless_of_notify_when_clean(sns):
    # The default fixtures already have failed pipelines; NOTIFY_WHEN_CLEAN unset
    # (the default) must never suppress an actionable report.
    assert "NOTIFY_WHEN_CLEAN" not in status_report.os.environ

    report = status_report.lambda_handler({}, None)

    assert report["failed"]
    assert len(sns.messages) == 1


def test_an_incomplete_head_is_always_published_regardless_of_notify_when_clean(cp, sns):
    probe = cp.pipelines[PROBE][0]
    probe["sourceRevisions"] = [r for r in probe["sourceRevisions"] if r["actionName"] == GLOBAL]

    report = status_report.lambda_handler({}, None)

    assert report["head_complete"] is False
    assert len(sns.messages) == 1


def test_drift_on_a_running_pipeline_is_still_reported(cp, sns):
    """Item 10: a report whose only content is active drift was suppressed.

    333333333333 is behind HEAD with an execution in flight. It is excluded from
    ``drifted`` on purpose - it is already being remediated - but that left the
    scheduled report with nothing actionable, so nothing was published, and the
    drift check had skipped it too. Between them the account was invisible.
    """
    current = cp.pipelines["111111111111-customizations-pipeline"]
    for name in ("222222222222", "444444444444", "555555555555"):
        cp.pipelines[f"{name}-customizations-pipeline"] = current

    report = status_report.lambda_handler({}, None)

    assert report["failed"] == []
    assert report["drifted"] == []
    assert [s["pipeline"] for s in report["drifted_running"]] == [
        "333333333333-customizations-pipeline"
    ]
    assert len(sns.messages) == 1
    assert sns.messages[0]["subject"] == (
        "AFT pipeline report: 1 behind HEAD and still running"
    )
    assert "Behind HEAD, run already in flight" in sns.messages[0]["message"]


def test_one_uninspectable_pipeline_does_not_abort_the_report(cp, sns):
    cp.inspect_errors = {"222222222222-customizations-pipeline"}

    report = status_report.lambda_handler({}, None)

    assert [e["pipeline"] for e in report["inspect_errors"]] == [
        "222222222222-customizations-pipeline"
    ]
    assert report["total"] == 4
    assert "could not be inspected" in sns.messages[0]["subject"]
    assert "Could not be inspected" in sns.messages[0]["message"]


def test_an_uninspectable_pipeline_is_always_published(cp, sns):
    """An all-clear report that silently dropped a pipeline is not an all-clear."""
    current = cp.pipelines["111111111111-customizations-pipeline"]
    for name in list(cp.pipelines):
        if name.endswith("-customizations-pipeline"):
            cp.pipelines[name] = current
    cp.inspect_errors = {"222222222222-customizations-pipeline"}

    report = status_report.lambda_handler({}, None)

    assert report["failed"] == []
    assert report["drifted"] == []
    assert len(sns.messages) == 1
    assert "could not be inspected" in sns.messages[0]["subject"]
