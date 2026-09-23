"""Publish a status report for the AFT customizations pipelines to SNS.

Runs on its own schedule, a few hours after the daily drift check, so the
report describes the *outcome* of the runs the drift check triggered: which
pipelines failed, which are still behind HEAD, and which are still running.
"""

from __future__ import annotations

import json
import logging
import os

import boto3

from aft_pipelines import (
    ACTIVE_STATES,
    DEFAULT_PIPELINE_PATTERN,
    configure_logging,
    env_flag,
    head_is_complete,
    head_revisions,
    inspect_pipelines,
    list_aft_pipelines,
    publish,
    short,
    source_actions,
)

logger = logging.getLogger(__name__)
configure_logging()

codepipeline = boto3.client("codepipeline")
sns = boto3.client("sns")

#: "Superseded" is not a failure - a newer execution simply replaced it.
FAILED_STATES = frozenset({"Failed", "Stopped", "Cancelled"})


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
    """Entry point. Builds the report and, unless it is clean, publishes it to SNS."""
    report = build_report()
    if env_flag("NOTIFY_WHEN_CLEAN") or _is_actionable(report):
        publish(
            sns,
            os.environ.get("SNS_TOPIC_ARN", ""),
            _subject(report),
            _format_message(report),
        )
    logger.info("Status report: %s", json.dumps(report, default=str))
    return report


def _is_actionable(report: dict) -> bool:
    """Whether the report contains something a human would want to act on.

    A report is a non-event only when every pipeline is current and HEAD was
    fully resolved - failures, drift, an unavailable or partial HEAD, a pipeline
    that could not be inspected, and no pipeline matching the configured pattern
    are all actionable and must never be suppressed by NOTIFY_WHEN_CLEAN.

    Drift on a pipeline that is still running counts too. It is being remediated,
    which is why it is not in ``drifted``, but a report whose only content is
    active drift is still a report somebody asked for - and suppressing it is
    what made a stale pipeline stuck in ``InProgress`` invisible in both the
    drift check and the report that follows it.
    """
    return bool(
        not report["total"]
        or report["failed"]
        or report["drifted"]
        or report["drifted_running"]
        or report["inspect_errors"]
        or not report["head_complete"]
    )


def build_report() -> dict:
    """Collect the latest state of every AFT customizations pipeline."""
    probe_pipeline = os.environ["PROBE_PIPELINE_NAME"]
    pattern = os.environ.get("PIPELINE_NAME_PATTERN", DEFAULT_PIPELINE_PATTERN)

    head = head_revisions(codepipeline, probe_pipeline)
    # A pipeline that cannot be inspected is recorded and skipped: one deletion
    # race or throttled API call must not cost the whole estate's report.
    statuses, inspect_errors = inspect_pipelines(
        codepipeline, list_aft_pipelines(codepipeline, pattern), head
    )
    return {
        "head_revisions": head,
        # A probe execution read mid-flight can carry only one of the two source
        # revisions. Drift judged against that half-HEAD reports pipelines as
        # current on the action that is missing, so flag it and never claim
        # currency below.
        "head_complete": head_is_complete(head, set(source_actions())),
        "pattern": pattern,
        "total": len(statuses),
        "failed": [s for s in statuses if s["status"] in FAILED_STATES],
        "running": [s for s in statuses if s["status"] in ACTIVE_STATES],
        "drifted": [s for s in statuses if s["drifted"] and s["status"] not in ACTIVE_STATES],
        "drifted_running": [s for s in statuses if s["drifted"] and s["status"] in ACTIVE_STATES],
        "inspect_errors": inspect_errors,
        "statuses": statuses,
    }


def _subject(report: dict) -> str:
    failed, drifted = len(report["failed"]), len(report["drifted"])
    suffix = (
        f", {len(report['inspect_errors'])} could not be inspected"
        if report["inspect_errors"]
        else ""
    )
    if not report["total"]:
        # "all 0 pipelines current" reads as healthy; no pipelines matched at all.
        return f"AFT pipeline report: no pipelines found matching {report['pattern']}{suffix}"
    if not report["head_revisions"]:
        # Without HEAD nothing can be judged as current, so never say it is.
        return f"AFT pipeline report: HEAD unavailable, {failed} failed{suffix}"
    if not report["head_complete"]:
        # Half a HEAD is not ground truth for drift either.
        return f"AFT pipeline report: HEAD incomplete, {failed} failed{suffix}"
    if failed or drifted:
        return f"AFT pipeline report: {failed} failed, {drifted} behind HEAD{suffix}"
    if report["drifted_running"]:
        # Nothing to act on yet, but not "all current" either: these accounts are
        # behind HEAD with a run in flight to fix them.
        return (
            f"AFT pipeline report: {len(report['drifted_running'])} behind HEAD "
            f"and still running{suffix}"
        )
    return f"AFT pipeline report: all {report['total']} pipelines current{suffix}"


def _head_line(report: dict) -> str:
    if not report["head_revisions"]:
        return "HEAD revisions: unavailable (probe pipeline has not run)"
    if not report["head_complete"]:
        return "HEAD revisions: incomplete (probe resolved only some sources), drift not judged"
    return "HEAD revisions:"


def _format_message(report: dict) -> str:
    head = report["head_revisions"]
    lines = [
        "AFT customizations pipeline status report",
        "",
        _head_line(report),
        *[f"  {action}: {short(revision)}" for action, revision in sorted(head.items())],
        "",
        f"Pipelines:     {report['total']}",
        f"Failed:        {len(report['failed'])}",
        f"Still running: {len(report['running'])}",
        f"Behind HEAD:   {len(report['drifted'])}",
    ]
    for label, key in (
        ("Failed", "failed"),
        ("Still running", "running"),
        ("Behind HEAD", "drifted"),
        ("Behind HEAD, run already in flight", "drifted_running"),
    ):
        if report[key]:
            lines += ["", f"{label}:"]
            lines += [
                f"  {s['pipeline']} [{s['status']}] last run {s['last_execution_at'] or 'never'}"
                for s in report[key]
            ]
    if report["inspect_errors"]:
        lines += [
            "",
            "Could not be inspected - excluded from every count above:",
            *[f"  {e['pipeline']}: {e['error']}" for e in report["inspect_errors"]],
        ]
    return "\n".join(lines)
