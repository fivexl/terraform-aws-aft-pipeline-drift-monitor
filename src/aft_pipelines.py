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
resolves HEAD from GitHub on our behalf and reports it in the execution's
source revisions. There is no public CodeConnections API to read a commit from
a connected repository, so borrowing CodePipeline's own resolution is the only
way to learn HEAD without a separate GitHub credential.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

#: Output artifact names carry a ``source-`` prefix while action names do not.
_ARTIFACT_PREFIX = "source-"

SUCCEEDED = "Succeeded"
FAILED = "Failed"
#: Execution states that mean "this pipeline is already doing something".
ACTIVE_STATES = frozenset({"InProgress", "Stopping"})

DEFAULT_PIPELINE_PATTERN = r"^\d{12}-customizations-pipeline$"


def canonical_action(name: str) -> str:
    """Normalise an artifact or action name to the source action name."""
    return name[len(_ARTIFACT_PREFIX) :] if name.startswith(_ARTIFACT_PREFIX) else name


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


def revisions_from_summary(summary: dict[str, Any]) -> dict[str, str]:
    """Extract ``{action_name: commit_id}`` from a pipeline execution summary."""
    return {
        canonical_action(rev["actionName"]): rev["revisionId"]
        for rev in summary.get("sourceRevisions", [])
        if rev.get("actionName") and rev.get("revisionId")
    }


def executions(client: Any, pipeline: str, limit: int = 10) -> list[dict[str, Any]]:
    """Return the most recent execution summaries for ``pipeline``, newest first."""
    response = client.list_pipeline_executions(pipelineName=pipeline, maxResults=limit)
    return response.get("pipelineExecutionSummaries", [])


def head_revisions(client: Any, probe_pipeline: str, execution_id: str | None = None) -> dict[str, str]:
    """Resolve HEAD per source action from the revision probe pipeline.

    ``execution_id`` is the probe execution we are running inside; when omitted
    (direct invocation) the most recent probe execution is used instead.
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


def pipeline_status(client: Any, pipeline: str, head: dict[str, str]) -> dict[str, Any]:
    """Summarise one pipeline: latest state, applied commits and drift.

    ``drifted`` is judged against the last *successful* execution, because that
    is the commit the account is actually running. A pipeline that has never
    succeeded is drifted by definition.
    """
    summaries = executions(client, pipeline)
    latest = summaries[0] if summaries else None
    succeeded = next((s for s in summaries if s.get("status") == SUCCEEDED), None)
    applied = revisions_from_summary(succeeded) if succeeded else {}
    drifted = any(applied.get(action) != revision for action, revision in head.items())
    return {
        "pipeline": pipeline,
        "status": latest.get("status") if latest else "NeverExecuted",
        "last_execution_at": _isoformat(latest.get("startTime")) if latest else None,
        "applied_revisions": applied,
        "drifted": drifted,
        "active": bool(latest and latest.get("status") in ACTIVE_STATES),
    }


def _isoformat(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else value


def publish(client: Any, topic_arn: str, subject: str, message: str) -> None:
    """Publish to SNS, truncating to the service limits."""
    if not topic_arn:
        logger.warning("No SNS topic configured, dropping message: %s", subject)
        return
    client.publish(TopicArn=topic_arn, Subject=subject[:100], Message=message[:262144])
