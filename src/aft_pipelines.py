"""Shared helpers for inspecting AFT customizations pipelines.

AFT creates one CodePipeline per vended account, named
``<account-id>-customizations-pipeline``, with two ``CodeStarSourceConnection``
source actions (``aft-global-customizations`` and ``aft-account-customizations``)
configured with ``DetectChanges = false``. Because change detection is off, a
pipeline keeps whatever commit it last ran with until something starts it again,
so accounts silently fall behind the customizations repositories.

These helpers answer two questions:

* what commit does each pipeline currently sit on (per source action), and
* what commit is HEAD on each source repository.

HEAD comes from the *revision probe* pipeline that this module creates: it uses
the same CodeConnections connection and the same repositories, so CodePipeline
resolves HEAD from GitHub on our behalf. The drift detector reads it out of its
own job event (``revisions_from_job_artifacts``); everything else that needs HEAD
is not a CodePipeline action and reads the probe's latest execution instead
(``head_revisions``). There is no public CodeConnections API to read a commit
from a connected repository, so borrowing CodePipeline's own resolution is the
only way to learn HEAD without a separate GitHub credential.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Sequence
from typing import Any

logger = logging.getLogger(__name__)

#: AFT's own pipelines prefix output artifact names with ``source-``; the probe
#: pipeline this module creates does not. Normalising both keeps the keys
#: comparable across the two.
_ARTIFACT_PREFIX = "source-"

SUCCEEDED = "Succeeded"
FAILED = "Failed"
#: Execution states that mean "this pipeline is already doing something".
ACTIVE_STATES = frozenset({"InProgress", "Stopping"})

DEFAULT_PIPELINE_PATTERN = r"^\d{12}-customizations-pipeline$"


def canonical_action(name: str) -> str:
    """Normalise an artifact or action name to the source action name."""
    return name.removeprefix(_ARTIFACT_PREFIX)


def short(revision: str | None) -> str:
    """Abbreviate a commit id for human-readable reports."""
    return revision[:8] if revision else "unknown"


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def source_actions() -> list[str]:
    """Source action names the module tracks, from the SOURCE_ACTIONS env var."""
    raw = os.environ.get("SOURCE_ACTIONS", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def configure_logging() -> None:
    """Set the root log level from LOG_LEVEL (Lambda owns the handler)."""
    logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())


def list_aft_pipelines(client: Any, pattern: str = DEFAULT_PIPELINE_PATTERN) -> list[str]:
    """Return the names of every pipeline matching ``pattern``, sorted."""
    matcher = re.compile(pattern)
    names: list[str] = []
    for page in client.get_paginator("list_pipelines").paginate():
        names.extend(p["name"] for p in page.get("pipelines", []) if matcher.match(p["name"]))
    return sorted(names)


def aft_pipeline_source_actions(client: Any, pipeline: str) -> set[str]:
    """Read the source action names AFT actually configured on ``pipeline``.

    This is the only source of truth for the action names drift is judged on.
    Drift is decided by comparing per-action commit ids, keyed by source action
    name, so if AFT ever renames a source action or adds a third one, every
    pipeline's applied revisions stop matching HEAD's keys and all of them look
    permanently drifted - which would start every pipeline, every day.

    The probe pipeline this module creates cannot detect that: its own source
    action names come from this module's Terraform (``local.probe_sources`` ->
    ``SOURCE_ACTIONS``), so comparing the probe's resolved names against
    ``source_actions()`` compares the module's configuration with itself.
    ``GetPipeline`` on a real AFT customizations pipeline reads AFT's side of
    the contract instead, which is the half that can actually change.

    Actions are matched on ``actionTypeId.category == "Source"`` rather than on
    the stage being called ``Source``, so a stage rename does not hide them, and
    names go through :func:`canonical_action` for the same reason
    :func:`revisions_from_summary` does.
    """
    definition = client.get_pipeline(name=pipeline)["pipeline"]
    return {
        canonical_action(action["name"])
        for stage in definition.get("stages", [])
        for action in stage.get("actions", [])
        if action.get("name") and action.get("actionTypeId", {}).get("category") == "Source"
    }


def revisions_from_summary(summary: dict[str, Any]) -> dict[str, str]:
    """Extract ``{action_name: commit_id}`` from a pipeline execution summary."""
    return {
        canonical_action(rev["actionName"]): rev["revisionId"]
        for rev in summary.get("sourceRevisions", [])
        if rev.get("actionName") and rev.get("revisionId")
    }


def revisions_from_job_artifacts(job: dict[str, Any]) -> dict[str, str]:
    """Extract ``{action_name: commit_id}`` from a CodePipeline Lambda job event.

    CodePipeline delivers every input artifact's resolved commit id in the job
    event itself, as ``data.inputArtifacts[].revision`` - the GitHub commit id
    for ``CodeStarSourceConnection`` sources. That makes the probe execution's
    own HEAD readable without looking up any execution history, which matters
    because the Lambda action event omits ``pipelineContext`` entirely, so there
    is no execution id to look up. See
    https://docs.aws.amazon.com/codepipeline/latest/userguide/action-reference-Lambda.html.

    Returns ``{}`` when there is no job or its artifacts carry no revisions, so
    callers can fall back to ``head_revisions``.
    """
    return {
        canonical_action(artifact["name"]): artifact["revision"]
        for artifact in job.get("data", {}).get("inputArtifacts", [])
        if artifact.get("name") and artifact.get("revision")
    }


def executions(client: Any, pipeline: str, limit: int = 10) -> list[dict[str, Any]]:
    """Return the most recent execution summaries for ``pipeline``, newest first."""
    response = client.list_pipeline_executions(pipelineName=pipeline, maxResults=limit)
    return response.get("pipelineExecutionSummaries", [])


def head_revisions(client: Any, probe_pipeline: str, execution_id: str | None = None) -> dict[str, str]:
    """Resolve HEAD per source action from the revision probe pipeline's history.

    For callers that are *not* a CodePipeline action - the status report, the
    weekly full run, and local/manual invocation - and so have no job event to
    read revisions out of. ``execution_id`` pins a specific probe execution; when
    omitted the most recent one that recorded revisions is used.
    """
    if execution_id:
        revisions = _execution_revisions(client, probe_pipeline, execution_id)
        if revisions:
            return revisions
        # The execution is still in progress, so fall back to the summary, which
        # carries the same revisions once the source stage has completed.
        for summary in executions(client, probe_pipeline, limit=5):
            if summary.get("pipelineExecutionId") == execution_id:
                return revisions_from_summary(summary)

    for summary in executions(client, probe_pipeline, limit=5):
        revisions = revisions_from_summary(summary)
        if revisions:
            logger.info(
                "Resolved HEAD from probe execution %s started %s",
                summary.get("pipelineExecutionId"),
                _isoformat(summary.get("startTime")),
            )
            return revisions
    return {}


def _execution_revisions(client: Any, pipeline: str, execution_id: str) -> dict[str, str]:
    execution = client.get_pipeline_execution(
        pipelineName=pipeline, pipelineExecutionId=execution_id
    )["pipelineExecution"]
    return {
        canonical_action(rev["name"]): rev["revisionId"]
        for rev in execution.get("artifactRevisions", [])
        if rev.get("name") and rev.get("revisionId")
    }


def head_is_complete(head: dict[str, str], expected: set[str]) -> bool:
    """Whether ``head`` covers exactly the source actions the module tracks.

    A probe execution inspected mid-flight can carry only *one* of the two source
    revisions, and drift judged against half a HEAD is worse than none: pipelines
    look current on the action that is missing. An empty ``expected`` means
    SOURCE_ACTIONS is unset and there is nothing to validate against, so any
    resolved head passes; an empty ``head`` never does.
    """
    return bool(head) and (not expected or set(head) == expected)


def drifted_against(revisions: dict[str, str], head: dict[str, str]) -> bool:
    """Whether ``revisions`` differs from ``head`` on any tracked action."""
    return any(revisions.get(action) != revision for action, revision in head.items())


def pipeline_status(client: Any, pipeline: str, head: dict[str, str]) -> dict[str, Any]:
    """Summarise one pipeline: latest state, applied commits and drift.

    ``drifted`` is judged against the last *successful* execution, because that
    is the commit the account is actually running. A pipeline with no success
    among the executions inspected is drifted by definition; ``no_success_found``
    distinguishes that case, and ``failed_on_head`` marks the pipelines whose
    newest attempt already ran HEAD and failed, which a restart cannot fix.
    """
    summaries = executions(client, pipeline)
    latest = summaries[0] if summaries else None
    succeeded = next((s for s in summaries if s.get("status") == SUCCEEDED), None)
    applied = revisions_from_summary(succeeded) if succeeded else {}
    attempted = revisions_from_summary(latest) if latest else {}
    drifted = drifted_against(applied, head)
    latest_status = latest.get("status") if latest else "NeverExecuted"
    return {
        "pipeline": pipeline,
        "status": latest_status,
        "last_execution_at": _isoformat(latest.get("startTime")) if latest else None,
        "applied_revisions": applied,
        "drifted": drifted,
        # No success within the window this inspects - distinct from drift
        # against a known older commit, and not fixable by another run.
        "no_success_found": succeeded is None,
        # The most recent attempt already carried HEAD and did not succeed, so
        # restarting it would only repeat the same failure. Judged on ``Failed``
        # alone: ``Superseded`` means a newer execution replaced this one and
        # ``Stopped``/``Cancelled`` mean it never got to finish, so none of the
        # three is evidence that HEAD cannot be applied - treating them as such
        # suppresses exactly the retry that would bring the account up to date.
        "failed_on_head": (
            bool(head) and not drifted_against(attempted, head) and latest_status == FAILED
        ),
        "active": bool(latest and latest.get("status") in ACTIVE_STATES),
    }


def inspect_pipelines(
    client: Any, pipelines: list[str], head: dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Summarise every pipeline, isolating a failure to the pipeline that caused it.

    Returns ``(statuses, errors)``. A deletion race, a throttle, or a malformed
    pipeline used to abort the whole estate-level run: one account's transient
    API error meant no other account was inspected, remediated or reported. Here
    the failure is recorded against its own pipeline and the remaining ones are
    still processed, so callers can report a partial result honestly instead of
    losing the run.
    """
    statuses: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for name in pipelines:
        try:
            statuses.append(pipeline_status(client, name, head))
        except Exception as exc:  # one bad pipeline must not abort the estate
            logger.exception("Could not inspect %s", name)
            errors.append({"pipeline": name, "error": str(exc)})
    return statuses, errors


def validate_source_actions(
    client: Any, pipelines: list[str], expected: set[str]
) -> dict[str, str]:
    """Return ``{pipeline: reason}`` for every pipeline that must not be judged.

    Drift is decided by comparing per-action commit ids keyed on source action
    name, so a pipeline whose source actions are not exactly ``expected`` cannot
    be compared against HEAD at all: its applied revisions would never match
    HEAD's keys and it would look permanently behind, restarting on every run.

    Every pipeline is checked, not a sample. AFT generates them from one
    template, but a single hand-edited or partially-upgraded pipeline is the case
    that matters - sampling the first one either blocks remediation for the whole
    estate because of one outlier, or lets an incompatible later pipeline through
    unvalidated. The cost is one ``GetPipeline`` per pipeline per run.

    A pipeline whose definition cannot be read is quarantined too: an unreadable
    definition is not evidence that its source actions still match. An empty
    ``expected`` means SOURCE_ACTIONS is unset and there is nothing to validate
    against, so nothing is quarantined.
    """
    if not expected:
        return {}
    quarantined: dict[str, str] = {}
    for name in pipelines:
        try:
            actual = aft_pipeline_source_actions(client, name)
        except Exception as exc:
            logger.exception("Could not read the pipeline definition for %s", name)
            quarantined[name] = f"could not read its definition: {exc}"
            continue
        if actual != expected:
            quarantined[name] = (
                f"source actions {sorted(actual)} do not match the tracked "
                f"{sorted(expected)}"
            )
    return quarantined


def _isoformat(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else value


########################################################################
# Re-invoking customizations through AFT's own state machine
#
# AFT orchestrates a customizations re-run with the `aft-invoke-customizations`
# state machine, which this module hands the target accounts to instead of
# calling StartPipelineExecution itself. Two things come with that, neither of
# which a caller can reproduce from outside:
#
#   * Concurrency. The state machine is not a per-run cap but a backpressure
#     loop: Get Pipeline Executions -> Below Maximum Execution Threshold? ->
#     Execute Pipelines -> Wait 30s -> recheck, against AFT's own
#     `maximum_concurrent_customizations`. It starts as many accounts as that
#     budget allows and waits for the rest, indefinitely, so no cap of this
#     module's own is needed and none can be more correct.
#   * Completion. Direct starts bypass AFT's audit record and its own
#     success/failure notifications entirely.
#
# Requires AFT >= 1.21.0 for `bypass_steps`. Terraform asserts that floor at plan
# time; there is deliberately no runtime fallback, because without bypass_steps
# the state machine's Check Bypass state falls through to a DISTRIBUTED Map that
# runs the full provisioning framework for every account - far heavier than
# anything this module is asking for, and silently so.
########################################################################

#: AFT creates the state machine with this fixed name (modules/aft-customizations).
AFT_INVOKE_CUSTOMIZATIONS = "aft-invoke-customizations"

#: Runs the customizations pipelines without re-running provisioning bootstrap.
#: AFT 1.21.0+; the Check Bypass state that reads it uses JSONata and cannot be
#: backported.
BYPASS_PROVISIONING_BOOTSTRAP = "provisioning_bootstrap"

#: Step Functions caps an execution name at 80 characters and rejects whitespace,
#: control characters and the set <>{}[]?*"#%\^|~`$&,;:/ - so names are built from
#: the safe subset only.
_MAX_EXECUTION_NAME = 80
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]+")
_LEADING_ACCOUNT_ID = re.compile(r"^(\d{12})")


def account_id_from_pipeline(pipeline: str) -> str | None:
    """The account id AFT named ``pipeline`` after, or None if it has none.

    AFT names every customizations pipeline ``<account-id>-customizations-pipeline``,
    so the target account is readable from the name with no extra API call. A
    caller that overrode ``pipeline_name_pattern`` with a shape that does not
    start with the account id gets None, and must report that pipeline rather
    than guess: the state machine selects accounts, not pipelines.
    """
    match = _LEADING_ACCOUNT_ID.match(pipeline)
    return match.group(1) if match else None


def customizations_input(accounts: Sequence[str] | None = None) -> dict[str, Any]:
    """Build the state machine's input for ``accounts``, or for every AFT account.

    ``include`` is capped at 4 items by AFT's own request schema, so the account
    list travels as ONE ``accounts`` selector rather than one selector each.
    Passing no accounts selects ``all``, which resolves to every account in AFT's
    metadata table - more accurate than this module's pipeline-name pattern, and
    what the weekly full run wants.

    ``get_execution_id`` is deliberately absent even though the schema requires
    it: the state machine's first state injects it from ``$$.Execution.Name``,
    and a caller-supplied value would be overwritten.
    """
    include: list[dict[str, Any]] = (
        [{"type": "accounts", "target_value": sorted(accounts)}] if accounts else [{"type": "all"}]
    )
    return {"include": include, "bypass_steps": [BYPASS_PROVISIONING_BOOTSTRAP]}


def execution_name(prefix: str, invocation_key: str, payload: dict[str, Any]) -> str:
    """A deterministic execution name, which is what makes an invocation idempotent.

    Lambda can deliver an asynchronous event more than once, including after a
    successful run, so the same drift check can reach this module twice. Step
    Functions rejects a second ``StartExecution`` under a name that already
    exists, so deriving the name from the invocation's own id turns a duplicate
    delivery into a no-op instead of a second re-run of every account.

    The payload digest is part of the name on purpose: a retry that resolved a
    *different* set of accounts is new work and must not be suppressed.
    """
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:8]
    stem = _UNSAFE_NAME_CHARS.sub("-", f"{prefix}-{invocation_key}").strip("-")
    return f"{stem[: _MAX_EXECUTION_NAME - len(digest) - 1]}-{digest}"


def invoke_customizations(
    client: Any, state_machine_arn: str, payload: dict[str, Any], name: str
) -> dict[str, Any]:
    """Start the state machine, treating a duplicate name as already invoked.

    ``ExecutionAlreadyExists`` is the idempotency guarantee working, not a
    failure: some earlier delivery of this same invocation already handed these
    accounts to AFT.
    """
    logger.info("Invoking %s as %s with %s", state_machine_arn, name, json.dumps(payload))
    try:
        response = client.start_execution(
            stateMachineArn=state_machine_arn, name=name, input=json.dumps(payload)
        )
    except Exception as exc:
        if _error_code(exc) != "ExecutionAlreadyExists":
            raise
        logger.warning(
            "Execution %s already exists - a duplicate delivery of this invocation, "
            "so the accounts were already handed to AFT",
            name,
        )
        return {
            "execution_name": name,
            "execution_arn": _execution_arn(state_machine_arn, name),
            "already_invoked": True,
        }
    return {
        "execution_name": name,
        "execution_arn": response.get("executionArn"),
        "already_invoked": False,
    }


def _error_code(exc: Exception) -> str:
    """The AWS error code for ``exc``, falling back to its class name."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if code:
            return str(code)
    return exc.__class__.__name__


def _execution_arn(state_machine_arn: str, name: str) -> str:
    """The execution ARN for ``name``, which StartExecution does not return on conflict."""
    return f"{state_machine_arn.replace(':stateMachine:', ':execution:', 1)}:{name}"


def chatbot_envelope(subject: str, message: str) -> str:
    """Wrap a subject/message pair in Chatbot's custom notification schema.

    AWS Chatbot only renders default AWS service events or this schema; a plain
    string is silently discarded, with nothing logged unless the Chatbot
    configuration's LoggingLevel is ERROR. See
    https://docs.aws.amazon.com/chatbot/latest/adminguide/custom-notifs.html.
    ``title`` is capped at 250 characters and ``description`` at 8000, per the
    same reference.
    """
    return json.dumps(
        {
            "version": "1.0",
            "source": "custom",
            "content": {
                "textType": "client-markdown",
                "title": subject[:250],
                "description": message[:8000],
            },
        }
    )


def publish(client: Any, topic_arn: str, subject: str, message: str) -> None:
    """Publish to SNS, truncating to the service limits.

    Wraps the message in Chatbot's custom notification envelope when
    ENABLE_CHATBOT is set, so Slack delivery doesn't discard it. Left as plain
    text otherwise, so an email subscriber doesn't receive raw JSON.
    """
    if not topic_arn:
        logger.warning("No SNS topic configured, dropping message: %s", subject)
        return
    if env_flag("ENABLE_CHATBOT"):
        message = chatbot_envelope(subject, message)
    client.publish(TopicArn=topic_arn, Subject=subject[:100], Message=message[:262144])
