"""Unit tests for the shared helpers."""

from __future__ import annotations

import json

import aft_pipelines
from tests.conftest import ACCOUNT, GLOBAL, HEAD_ACCOUNT, HEAD_GLOBAL, OLD_GLOBAL, PROBE, summary


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
        "555555555555-customizations-pipeline",
    ]
    assert PROBE not in names
    assert "aft-account-request" not in names


def test_aft_pipeline_source_actions_reads_only_the_source_stage(cp):
    """The guard's input: AFT's own configured source action names."""
    assert aft_pipelines.aft_pipeline_source_actions(
        cp, "111111111111-customizations-pipeline"
    ) == {GLOBAL, ACCOUNT}
    assert cp.described == ["111111111111-customizations-pipeline"]


def test_aft_pipeline_source_actions_normalises_artifact_prefixed_names(cp):
    cp.source_action_names["111111111111-customizations-pipeline"] = [f"source-{GLOBAL}"]
    assert aft_pipelines.aft_pipeline_source_actions(
        cp, "111111111111-customizations-pipeline"
    ) == {GLOBAL}


def test_aft_pipeline_source_actions_sees_an_added_source(cp):
    cp.source_action_names["111111111111-customizations-pipeline"] = [GLOBAL, ACCOUNT, "aft-extra"]
    assert aft_pipelines.aft_pipeline_source_actions(
        cp, "111111111111-customizations-pipeline"
    ) == {GLOBAL, ACCOUNT, "aft-extra"}


def test_head_revisions_from_named_execution_normalises_artifact_names(cp):
    execution_id = cp.pipelines[PROBE][0]["pipelineExecutionId"]
    assert aft_pipelines.head_revisions(cp, PROBE, execution_id) == {
        GLOBAL: HEAD_GLOBAL,
        ACCOUNT: HEAD_ACCOUNT,
    }


def test_head_revisions_falls_back_to_latest_execution(cp):
    assert aft_pipelines.head_revisions(cp, PROBE) == {GLOBAL: HEAD_GLOBAL, ACCOUNT: HEAD_ACCOUNT}


def test_head_revisions_falls_back_to_the_summary_of_an_in_flight_execution(cp):
    # GetPipelineExecution has not recorded the artifact revisions yet, but the
    # execution summary already carries them.
    cp.hide_artifact_revisions = True
    execution_id = cp.pipelines[PROBE][0]["pipelineExecutionId"]

    assert aft_pipelines.head_revisions(cp, PROBE, execution_id) == {
        GLOBAL: HEAD_GLOBAL,
        ACCOUNT: HEAD_ACCOUNT,
    }


def test_head_revisions_empty_when_probe_never_ran(cp):
    cp.pipelines[PROBE] = []
    assert aft_pipelines.head_revisions(cp, PROBE) == {}


def test_head_is_complete_rejects_a_partial_probe_result():
    expected = {GLOBAL, ACCOUNT}
    assert aft_pipelines.head_is_complete({GLOBAL: HEAD_GLOBAL, ACCOUNT: HEAD_ACCOUNT}, expected)
    # Mid-execution the account source may not have resolved yet.
    assert not aft_pipelines.head_is_complete({GLOBAL: HEAD_GLOBAL}, expected)
    assert not aft_pipelines.head_is_complete({}, expected)
    # Renamed or extra action - the set no longer matches what we track.
    assert not aft_pipelines.head_is_complete({GLOBAL: HEAD_GLOBAL, "x": HEAD_ACCOUNT}, expected)
    # SOURCE_ACTIONS unset: nothing to validate against, any resolved head passes.
    assert aft_pipelines.head_is_complete({GLOBAL: HEAD_GLOBAL}, set())
    assert not aft_pipelines.head_is_complete({}, set())


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

    # Failed having already run HEAD: drifted, but a restart cannot help.
    on_head = aft_pipelines.pipeline_status(cp, "444444444444-customizations-pipeline", head)
    assert on_head["drifted"] is True
    assert on_head["status"] == "Failed"
    assert on_head["no_success_found"] is True
    assert on_head["failed_on_head"] is True

    # Failed on an older commit: worth another run.
    behind = aft_pipelines.pipeline_status(cp, "555555555555-customizations-pipeline", head)
    assert behind["drifted"] is True
    assert behind["no_success_found"] is True
    assert behind["failed_on_head"] is False


def test_only_a_failed_newest_execution_suppresses_a_retry(cp):
    """failed_on_head must not swallow Superseded, Stopped or Cancelled.

    All three mean HEAD was never successfully applied - a superseded execution
    was replaced by a newer one, and a stopped or cancelled one never finished -
    so treating them as "already failed on HEAD" suppresses exactly the retry
    that would bring the account up to date.
    """
    head = {GLOBAL: HEAD_GLOBAL, ACCOUNT: HEAD_ACCOUNT}
    name = "444444444444-customizations-pipeline"

    for state, suppresses in (
        ("Failed", True),
        ("Superseded", False),
        ("Stopped", False),
        ("Cancelled", False),
    ):
        cp.pipelines[name] = [summary(state, HEAD_GLOBAL, HEAD_ACCOUNT, minutes_ago=20)]
        status = aft_pipelines.pipeline_status(cp, name, head)
        assert status["drifted"] is True, state
        assert status["failed_on_head"] is suppresses, state


def test_validate_source_actions_checks_every_pipeline(cp):
    """Item 6: sampling the first pipeline either blocked the estate or let a
    later incompatible pipeline through unvalidated."""
    names = sorted(n for n in cp.pipelines if n.endswith("-customizations-pipeline"))
    # The outlier is deliberately NOT the first pipeline.
    cp.source_action_names[names[-1]] = [GLOBAL, "aft-renamed"]

    quarantined = aft_pipelines.validate_source_actions(cp, names, {GLOBAL, ACCOUNT})

    assert list(quarantined) == [names[-1]]
    assert "aft-renamed" in quarantined[names[-1]]
    assert sorted(cp.described) == names


def test_validate_source_actions_quarantines_an_unreadable_definition(cp):
    names = ["111111111111-customizations-pipeline", "222222222222-customizations-pipeline"]
    cp.describe_errors = {names[0]}

    quarantined = aft_pipelines.validate_source_actions(cp, names, {GLOBAL, ACCOUNT})

    assert "could not read its definition" in quarantined[names[0]]
    assert names[1] not in quarantined


def test_validate_source_actions_is_a_noop_without_tracked_actions(cp):
    """SOURCE_ACTIONS unset means there is nothing to validate against."""
    assert aft_pipelines.validate_source_actions(cp, ["111111111111-customizations-pipeline"], set()) == {}
    assert cp.described == []


def test_inspect_pipelines_isolates_a_failure_to_its_own_pipeline(cp):
    names = sorted(n for n in cp.pipelines if n.endswith("-customizations-pipeline"))
    cp.inspect_errors = {names[0]}

    statuses, errors = aft_pipelines.inspect_pipelines(cp, names, {})

    assert [s["pipeline"] for s in statuses] == names[1:]
    assert errors == [{"pipeline": names[0], "error": f"PipelineNotFoundException: {names[0]}"}]


def test_publish_is_a_noop_without_a_topic(sns):
    aft_pipelines.publish(sns, "", "subject", "message")
    assert sns.messages == []


def test_publish_sends_plain_text_by_default(sns, monkeypatch):
    monkeypatch.delenv("ENABLE_CHATBOT", raising=False)
    aft_pipelines.publish(sns, "arn:aws:sns:us-east-1:123456789012:topic", "subject", "message")
    assert sns.messages[-1]["message"] == "message"


def test_publish_wraps_in_chatbot_envelope_when_enabled(sns, monkeypatch):
    monkeypatch.setenv("ENABLE_CHATBOT", "true")
    aft_pipelines.publish(sns, "arn:aws:sns:us-east-1:123456789012:topic", "subject", "message")
    envelope = json.loads(sns.messages[-1]["message"])
    assert envelope == {
        "version": "1.0",
        "source": "custom",
        "content": {
            "textType": "client-markdown",
            "title": "subject",
            "description": "message",
        },
    }


def test_chatbot_envelope_truncates_to_service_limits():
    envelope = json.loads(aft_pipelines.chatbot_envelope("t" * 300, "d" * 9000))
    assert len(envelope["content"]["title"]) == 250
    assert len(envelope["content"]["description"]) == 8000
