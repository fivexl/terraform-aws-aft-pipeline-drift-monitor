"""Tests for the weekly full-run Lambda."""

from __future__ import annotations

import pytest

import run_all

RUNNING = "333333333333-customizations-pipeline"
#: Every idle pipeline, oldest last execution first.
BY_AGE = [
    "555555555555-customizations-pipeline",  # 40 minutes ago
    "222222222222-customizations-pipeline",  # 30
    "444444444444-customizations-pipeline",  # 20
    "111111111111-customizations-pipeline",  # 10
]


@pytest.fixture(autouse=True)
def wire_clients(monkeypatch, cp, sns):
    monkeypatch.setattr(run_all, "codepipeline", cp)
    monkeypatch.setattr(run_all, "sns", sns)


def test_starts_every_idle_pipeline_even_when_current(cp, sns):
    summary = run_all.lambda_handler({}, None)

    # 111111111111 is already at HEAD and still gets started: that is the point.
    assert sorted(cp.started) == sorted(BY_AGE)
    assert summary["skipped_already_running"] == [RUNNING]
    assert summary["pipelines_found"] == 5
    assert sns.messages[0]["subject"] == "AFT weekly full run: started 4 of 5 pipeline(s)"


def test_never_supersedes_a_running_execution(cp):
    run_all.lambda_handler({}, None)

    assert RUNNING not in cp.started


def test_oldest_run_goes_first_so_the_cap_rotates(monkeypatch, cp, sns):
    """Selecting by name would start the same accounts every week forever."""
    monkeypatch.setenv("MAX_PIPELINES_PER_RUN", "2")

    summary = run_all.lambda_handler({}, None)

    assert cp.started == BY_AGE[:2]
    assert summary["deferred_over_limit"] == BY_AGE[2:]
    assert sns.messages[0]["subject"] == "AFT weekly full run: started 2 of 5 pipeline(s)"


def test_dry_run_starts_nothing(monkeypatch, cp, sns):
    monkeypatch.setenv("DRY_RUN", "true")

    summary = run_all.lambda_handler({}, None)

    assert cp.started == []
    assert summary["started"] == []
    assert sns.messages[0]["subject"] == "AFT weekly full run: would start 4 of 5 pipeline(s)"
    assert "DRY_RUN is set" in sns.messages[0]["message"]


def test_one_unstartable_pipeline_does_not_abort_the_others(cp, sns):
    cp.start_errors = {BY_AGE[0]}

    summary = run_all.lambda_handler({}, None)

    assert cp.started == BY_AGE[1:]
    assert summary["failed_to_start"] == BY_AGE[:1]
    assert "Could not be started" in sns.messages[0]["message"]


def test_publish_failure_does_not_lose_the_run(sns):
    sns.fail = True

    summary = run_all.lambda_handler({}, None)

    assert sorted(summary["started"]) == sorted(BY_AGE)
    assert sns.messages == []


def test_ignores_non_aft_pipelines(cp):
    run_all.lambda_handler({}, None)

    assert "aft-account-request" not in cp.started
    assert not any(name.startswith("aft-pipeline-drift-monitor") for name in cp.started)
