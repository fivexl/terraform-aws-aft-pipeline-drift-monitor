"""Unit tests for the shared helpers."""

from __future__ import annotations

import aft_pipelines
from tests.conftest import ACCOUNT, GLOBAL, HEAD_ACCOUNT, HEAD_GLOBAL, OLD_GLOBAL, PROBE


def test_canonical_action_strips_artifact_prefix():
    assert aft_pipelines.canonical_action("source-aft-global-customizations") == GLOBAL
    assert aft_pipelines.canonical_action(GLOBAL) == GLOBAL


def test_list_aft_pipelines_matches_account_pipelines_only(cp):
    names = aft_pipelines.list_aft_pipelines(cp)
    assert names == [
        "111111111111-customizations-pipeline",
        "222222222222-customizations-pipeline",
        "333333333333-customizations-pipeline",
        "444444444444-customizations-pipeline",
    ]
    assert PROBE not in names
    assert "aft-account-request" not in names


def test_head_revisions_from_named_execution_normalises_artifact_names(cp):
    execution_id = cp.pipelines[PROBE][0]["pipelineExecutionId"]
    assert aft_pipelines.head_revisions(cp, PROBE, execution_id) == {
        GLOBAL: HEAD_GLOBAL,
        ACCOUNT: HEAD_ACCOUNT,
    }


def test_head_revisions_falls_back_to_latest_execution(cp):
    assert aft_pipelines.head_revisions(cp, PROBE) == {GLOBAL: HEAD_GLOBAL, ACCOUNT: HEAD_ACCOUNT}


def test_head_revisions_empty_when_probe_never_ran(cp):
    cp.pipelines[PROBE] = []
    assert aft_pipelines.head_revisions(cp, PROBE) == {}


def test_pipeline_status_judges_drift_against_last_success(cp):
    head = {GLOBAL: HEAD_GLOBAL, ACCOUNT: HEAD_ACCOUNT}

    current = aft_pipelines.pipeline_status(cp, "111111111111-customizations-pipeline", head)
    assert current["drifted"] is False
    assert current["active"] is False

    stale = aft_pipelines.pipeline_status(cp, "222222222222-customizations-pipeline", head)
    assert stale["drifted"] is True
    assert stale["applied_revisions"][GLOBAL] == OLD_GLOBAL

    # Running on HEAD, but the last *successful* run is stale: drifted and active.
    running = aft_pipelines.pipeline_status(cp, "333333333333-customizations-pipeline", head)
    assert running["drifted"] is True
    assert running["active"] is True

    never = aft_pipelines.pipeline_status(cp, "444444444444-customizations-pipeline", head)
    assert never["drifted"] is True
    assert never["status"] == "Failed"


def test_publish_is_a_noop_without_a_topic(sns):
    aft_pipelines.publish(sns, "", "subject", "message")
    assert sns.messages == []
