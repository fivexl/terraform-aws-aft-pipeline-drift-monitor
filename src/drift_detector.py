"""Detect AFT customizations pipelines running stale commits and re-run them.

Invoked as a CodePipeline ``Lambda`` action from the revision probe pipeline
this module creates: the probe's source stage resolves HEAD of both
customizations repositories through the existing CodeConnections connection,
then hands control here. Every AFT pipeline whose last successful execution
used an older commit is started (unless ``DRY_RUN`` is set).

Pipeline failures are reported to SNS by an EventBridge rule, not from here.
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
    head_revisions,
    list_aft_pipelines,
    pipeline_status,
    publish,
    short,
    start_pipelines,
)

logger = logging.getLogger(__name__)
configure_logging()

codepipeline = boto3.client("codepipeline")
sns = boto3.client("sns")


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
    """Entry point. Reports job success/failure when driven by CodePipeline."""
    job = (event or {}).get("CodePipeline.job") or {}
    job_id = job.get("id")
    try:
        result = detect_and_run(job)
    except Exception as exc:  # noqa: BLE001 - must not leave the pipeline hanging
        logger.exception("Drift detection failed")
        if job_id:
            # The pipeline goes FAILED, and the EventBridge failure rule alerts.
            codepipeline.put_job_failure_result(
                jobId=job_id,
                failureDetails={"type": "JobFailed", "message": str(exc)[:265]},
            )
            return {"error": str(exc)}
        publish(
            sns,
            os.environ.get("SNS_TOPIC_ARN", ""),
            "AFT pipeline drift check failed",
            f"The AFT pipeline drift detector raised an error:\n\n{exc}",
        )
        raise

    if job_id:
        codepipeline.put_job_success_result(jobId=job_id)
    return result


def detect_and_run(job: dict) -> dict:
    """Compare every AFT pipeline against HEAD and start the stale ones."""
    probe_pipeline = os.environ["PROBE_PIPELINE_NAME"]
    pattern = os.environ.get("PIPELINE_NAME_PATTERN", DEFAULT_PIPELINE_PATTERN)
    topic_arn = os.environ.get("SNS_TOPIC_ARN", "")
    dry_run = env_flag("DRY_RUN")
    max_runs = int(os.environ.get("MAX_PIPELINES_PER_RUN", "20"))

    context = job.get("data", {}).get("pipelineContext", {})
    execution_id = (
        context.get("pipelineExecutionId") if context.get("pipelineName") == probe_pipeline else None
    )

    head = head_revisions(codepipeline, probe_pipeline, execution_id)
    if not head:
        raise RuntimeError(
            f"Could not resolve HEAD revisions from probe pipeline {probe_pipeline}. "
            "Check the CodeConnections connection and the probe pipeline's source stage."
        )
    logger.info("HEAD revisions: %s", {k: short(v) for k, v in head.items()})

    pipelines = list_aft_pipelines(codepipeline, pattern)
    logger.info("Found %d AFT customizations pipeline(s)", len(pipelines))

    drifted, skipped = [], []
    for name in pipelines:
        status = pipeline_status(codepipeline, name, head)
        if not status["drifted"]:
            continue
        if status["active"]:
            skipped.append(status)
            continue
        drifted.append(status)

    started, deferred = start_pipelines(codepipeline, drifted, max_runs, dry_run)

    summary = {
        "probe_pipeline": probe_pipeline,
        "head_revisions": head,
        "pipelines_checked": len(pipelines),
        "drifted": [s["pipeline"] for s in drifted],
        "started": [s["pipeline"] for s in started],
        "skipped_already_running": [s["pipeline"] for s in skipped],
        "deferred_over_limit": [s["pipeline"] for s in deferred],
        "dry_run": dry_run,
    }
    logger.info("Drift check summary: %s", json.dumps(summary, default=str))

    if env_flag("NOTIFY_ON_DRIFT", True) and drifted:
        publish(
            sns,
            topic_arn,
            f"AFT drift check: {len(drifted)} pipeline(s) behind HEAD",
            _format_message(summary, head, drifted, skipped, deferred, dry_run),
        )
    return summary


def _format_message(summary, head, drifted, skipped, deferred, dry_run) -> str:
    to_run = len(drifted) - len(deferred)
    lines = [
        "AFT customizations pipeline drift check",
        "",
        "HEAD revisions:",
        *[f"  {action}: {short(revision)}" for action, revision in sorted(head.items())],
        "",
        f"Pipelines checked: {summary['pipelines_checked']}",
        f"Behind HEAD:       {len(drifted)}",
        f"{'Would start' if dry_run else 'Started'}:       {to_run if dry_run else len(summary['started'])}",
        "",
    ]
    for status in drifted:
        applied = ", ".join(
            f"{action}={short(status['applied_revisions'].get(action))}" for action in sorted(head)
        )
        lines.append(f"  {status['pipeline']} (last successful: {applied})")
    if skipped:
        lines += ["", "Skipped, already running:", *[f"  {s['pipeline']}" for s in skipped]]
    if deferred:
        lines += [
            "",
            "Deferred to the next run (MAX_PIPELINES_PER_RUN reached):",
            *[f"  {s['pipeline']}" for s in deferred],
        ]
    return "\n".join(lines)
