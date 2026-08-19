"""Tests for the weekly full-run Lambda."""

from __future__ import annotations

import pytest

import run_all

ALL_IDLE = [
    "111111111111-customizations-pipeline",
    "222222222222-customizations-pipeline",
    "444444444444-customizations-pipeline",
]
RUNNING = "333333333333-customizations-pipeline"


@pytest.fixture(autouse=True)
def wire_clients(monkeypatch, cp, sns):
    monkeypatch.setattr(run_all, "codepipeline", cp)
    monkeypatch.setattr(run_all, "sns", sns)


def test_starts_every_idle_pipeline_even_when_current(cp, sns):
    summary = run_all.lambda_handler({}, None)

    # 111111111111 is already at HEAD and still gets started: that is the point.
    assert cp.started == ALL_IDLE
    assert summary["started"] == ALL_IDLE
    assert summary["skipped_already_running"] == [RUNNING]
    assert summary["pipelines_found"] == 4
    assert sns.messages[0]["subject"] == "AFT weekly full run: started 3 of 4 pipeline(s)"


def test_never_supersedes_a_running_execution(cp):
    run_all.lambda_handler({}, None)

    assert RUNNING not in cp.started


def test_dry_run_starts_nothing(monkeypatch, cp, sns):
    monkeypatch.setenv("DRY_RUN", "true")

    summary = run_all.lambda_handler({}, None)

    assert cp.started == []
    assert summary["started"] == []
    assert sns.messages[0]["subject"] == "AFT weekly full run: would start 3 of 4 pipeline(s)"
    assert "DRY_RUN is set" in sns.messages[0]["message"]


def test_respects_max_pipelines_per_run(monkeypatch, cp, sns):
    monkeypatch.setenv("MAX_PIPELINES_PER_RUN", "2")

    summary = run_all.lambda_handler({}, None)

    assert cp.started == ALL_IDLE[:2]
    assert summary["deferred_over_limit"] == ALL_IDLE[2:]
    assert sns.messages[0]["subject"] == "AFT weekly full run: started 2 of 4 pipeline(s)"


def test_ignores_non_aft_pipelines(cp):
    run_all.lambda_handler({}, None)

    assert "aft-account-request" not in cp.started
    assert not any(name.startswith("aft-pipeline-drift-monitor") for name in cp.started)
