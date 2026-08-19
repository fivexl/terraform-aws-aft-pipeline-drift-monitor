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
    head_revisions,
    list_aft_pipelines,
    pipeline_status,
    publish,
    short,
)

logger = logging.getLogger(__name__)
configure_logging()

codepipeline = boto3.client("codepipeline")
sns = boto3.client("sns")

#: "Superseded" is not a failure - a newer execution simply replaced it.
FAILED_STATES = frozenset({"Failed", "Stopped", "Cancelled"})


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
    """Entry point. Builds the report and publishes it to SNS."""
    report = build_report()
    publish(
        sns,
        os.environ.get("SNS_TOPIC_ARN", ""),
        _subject(report),
        _format_message(report),
    )
    logger.info("Status report: %s", json.dumps(report, default=str))
    return report


def build_report() -> dict:
    """Collect the latest state of every AFT customizations pipeline."""
    probe_pipeline = os.environ["PROBE_PIPELINE_NAME"]
    pattern = os.environ.get("PIPELINE_NAME_PATTERN", DEFAULT_PIPELINE_PATTERN)

    head = head_revisions(codepipeline, probe_pipeline)
    statuses = [
        pipeline_status(codepipeline, name, head)
        for name in list_aft_pipelines(codepipeline, pattern)
    ]
    return {
        "head_revisions": head,
        "total": len(statuses),
        "failed": [s for s in statuses if s["status"] in FAILED_STATES],
        "running": [s for s in statuses if s["status"] in ACTIVE_STATES],
        "drifted": [s for s in statuses if s["drifted"] and s["status"] not in ACTIVE_STATES],
        "statuses": statuses,
    }


def _subject(report: dict) -> str:
    failed, drifted = len(report["failed"]), len(report["drifted"])
    if failed or drifted:
        return f"AFT pipeline report: {failed} failed, {drifted} behind HEAD"
    return f"AFT pipeline report: all {report['total']} pipelines current"


def _format_message(report: dict) -> str:
    head = report["head_revisions"]
    lines = [
        "AFT customizations pipeline status report",
        "",
        "HEAD revisions:" if head else "HEAD revisions: unavailable (probe pipeline has not run)",
        *[f"  {action}: {short(revision)}" for action, revision in sorted(head.items())],
        "",
        f"Pipelines:     {report['total']}",
        f"Failed:        {len(report['failed'])}",
        f"Still running: {len(report['running'])}",
        f"Behind HEAD:   {len(report['drifted'])}",
    ]
    for label, key in (("Failed", "failed"), ("Still running", "running"), ("Behind HEAD", "drifted")):
        if report[key]:
            lines += ["", f"{label}:"]
            lines += [
                f"  {s['pipeline']} [{s['status']}] last run {s['last_execution_at'] or 'never'}"
                for s in report[key]
            ]
    return "\n".join(lines)
