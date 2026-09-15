"""Start every AFT customizations pipeline, drift or not.

Runs on its own weekly schedule. Where the drift detector is surgical - it only
re-runs accounts whose last successful commit is behind HEAD - this is the
periodic baseline apply: it re-applies the customizations to every account, so
manual console changes, out-of-band edits and anything else that has drifted in
the *account* (rather than in the repository) gets corrected too.

Pipelines with an execution already in flight are skipped: the pipelines run in
SUPERSEDED mode, so a new execution would queue behind the running one and then
supersede it, which achieves nothing that the in-flight run is not already doing.

When more pipelines are eligible than MAX_PIPELINES_PER_RUN allows, the ones
whose last execution is oldest go first. Selecting by name instead would start
the same lexicographically-first accounts every week and never reach the tail.
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
    logger.info("Full run summary: %s", json.dumps(summary, default=str))
    if env_flag("NOTIFY_WHEN_CLEAN") or _is_actionable(summary):
        # A notification failure must not mask a run that started pipelines.
        try:
            publish(
                sns,
                os.environ.get("SNS_TOPIC_ARN", ""),
                _subject(summary),
                _format_message(summary),
            )
        except Exception:
            logger.exception("Could not publish the full run summary")
    return summary


def _is_actionable(summary: dict) -> bool:
    """Whether the summary contains something a human would want to act on.

    A summary is a non-event only when nothing was started and nothing failed
    to start - every eligible pipeline was already running, and there was
    nothing else to do. A dry run is always actionable regardless of outcome:
    a human running one wants to see what a real run would have done, not
    have that suppressed on the exact runs where nothing would have happened.
    No pipeline matching the configured pattern is also always actionable - a
    likely misconfiguration, not a clean run.
    """
    return bool(
        summary["dry_run"]
        or not summary["pipelines_found"]
        or summary["started"]
        or summary["failed_to_start"]
    )


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
    skipped = [s for s in statuses if s["active"]]
    # Oldest last execution first, so a cap below the account count rotates
    # through every account over successive runs instead of starving the tail.
    eligible = sorted(
        (s for s in statuses if not s["active"]),
        key=lambda s: (s["last_execution_at"] or "", s["pipeline"]),
    )

    started, deferred, failed_to_start = start_pipelines(codepipeline, eligible, max_runs, dry_run)

    return {
        "pipelines_found": len(pipelines),
        "started": [s["pipeline"] for s in started],
        "skipped_already_running": [s["pipeline"] for s in skipped],
        "deferred_over_limit": [s["pipeline"] for s in deferred],
        "failed_to_start": [s["pipeline"] for s in failed_to_start],
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
        ("Could not be started", "failed_to_start"),
    ):
        if summary[key]:
            lines += ["", f"{label}:", *[f"  {name}" for name in summary[key]]]
    if summary["dry_run"]:
        lines += ["", "DRY_RUN is set: nothing was actually started."]
    return "\n".join(lines)
