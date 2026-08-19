"""Start every AFT customizations pipeline, drift or not.

Runs on its own weekly schedule. Where the drift detector is surgical - it only
re-runs accounts whose last successful commit is behind HEAD - this is the
periodic baseline apply: it re-applies the customizations to every account, so
manual console changes, out-of-band edits and anything else that has drifted in
the *account* (rather than in the repository) gets corrected too.

Pipelines with an execution already in flight are skipped, because starting one
supersedes the running execution and would abort a half-applied Terraform run.
"""

from __future__ import annotations

import json
import logging
import os

import boto3

from aft_pipelines import (
    DEFAULT_PIPELINE_PATTERN,
    configure_logging,
    env_flag,
    list_aft_pipelines,
    pipeline_status,
    publish,
    start_pipelines,
)

logger = logging.getLogger(__name__)
configure_logging()

codepipeline = boto3.client("codepipeline")
sns = boto3.client("sns")


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
    """Entry point. Starts every idle AFT customizations pipeline."""
    summary = run_all()
    publish(sns, os.environ.get("SNS_TOPIC_ARN", ""), _subject(summary), _format_message(summary))
    logger.info("Full run summary: %s", json.dumps(summary, default=str))
    return summary


def run_all() -> dict:
    """Start every AFT customizations pipeline that is not already running."""
    pattern = os.environ.get("PIPELINE_NAME_PATTERN", DEFAULT_PIPELINE_PATTERN)
    dry_run = env_flag("DRY_RUN")
    max_runs = int(os.environ.get("MAX_PIPELINES_PER_RUN", "20"))

    pipelines = list_aft_pipelines(codepipeline, pattern)
    logger.info("Found %d AFT customizations pipeline(s)", len(pipelines))

    # No HEAD comparison: an empty head means pipeline_status reports state and
    # liveness only, which is all this Lambda needs.
    statuses = [pipeline_status(codepipeline, name, {}) for name in pipelines]
    eligible = [s for s in statuses if not s["active"]]
    skipped = [s for s in statuses if s["active"]]

    started, deferred = start_pipelines(codepipeline, eligible, max_runs, dry_run)

    return {
        "pipelines_found": len(pipelines),
        "started": [s["pipeline"] for s in started],
        "skipped_already_running": [s["pipeline"] for s in skipped],
        "deferred_over_limit": [s["pipeline"] for s in deferred],
        "dry_run": dry_run,
        "eligible": [s["pipeline"] for s in eligible],
    }


def _subject(summary: dict) -> str:
    verb = "would start" if summary["dry_run"] else "started"
    count = len(summary["eligible"]) - len(summary["deferred_over_limit"])
    return f"AFT weekly full run: {verb} {count} of {summary['pipelines_found']} pipeline(s)"


def _format_message(summary: dict) -> str:
    count = len(summary["eligible"]) - len(summary["deferred_over_limit"])
    lines = [
        "AFT customizations weekly full run - every pipeline, drift or not.",
        "",
        f"Pipelines found: {summary['pipelines_found']}",
        f"{'Would start' if summary['dry_run'] else 'Started'}:    {count}",
        f"Skipped (running): {len(summary['skipped_already_running'])}",
    ]
    for label, key in (
        ("Started", "started"),
        ("Skipped, already running", "skipped_already_running"),
        ("Deferred to the next run (MAX_PIPELINES_PER_RUN reached)", "deferred_over_limit"),
    ):
        if summary[key]:
            lines += ["", f"{label}:", *[f"  {name}" for name in summary[key]]]
    if summary["dry_run"]:
        lines += ["", "DRY_RUN is set: nothing was actually started."]
    return "\n".join(lines)
