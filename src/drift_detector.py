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
    head_is_complete,
    head_revisions,
    list_aft_pipelines,
    pipeline_status,
    publish,
    short,
    source_actions,
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
    except Exception as exc:  # must not leave the CodePipeline job hanging
        logger.exception("Drift detection failed")
        if job_id and _report_job(job_id, exc):
            # The pipeline goes FAILED, and the EventBridge failure rule alerts.
            return {"error": str(exc)}
        # No job to answer, or answering it failed: alert directly instead.
        publish(
            sns,
            os.environ.get("SNS_TOPIC_ARN", ""),
            "AFT pipeline drift check failed",
            f"The AFT pipeline drift detector raised an error:\n\n{exc}",
        )
        if not job_id:
            raise
        return {"error": str(exc)}

    if job_id and not _report_job(job_id):
        # The work is done, but CodePipeline was never told. The action now hangs
        # until its own timeout, which no EventBridge failure rule reports in the
        # meantime - so alert directly. Best effort: the result still stands.
        try:
            publish(
                sns,
                os.environ.get("SNS_TOPIC_ARN", ""),
                "AFT drift check succeeded but CodePipeline was not notified",
                "The AFT pipeline drift check completed successfully, but reporting "
                f"success for CodePipeline job {job_id} failed.\n\n"
                "The probe pipeline's Detect-Drift action is now unanswered and will "
                "hang until its action timeout expires. Any drifted pipelines were "
                "already started, so no drift check needs to be repeated - see the "
                "Lambda logs for the underlying PutJobSuccessResult error.",
            )
        except Exception:
            logger.exception("Could not publish the unreported job-success alert")
    return result


def _report_job(job_id: str, exc: Exception | None = None) -> bool:
    """Answer the CodePipeline job. Returns False if the answer could not be sent."""
    try:
        if exc is None:
            codepipeline.put_job_success_result(jobId=job_id)
        else:
            codepipeline.put_job_failure_result(
                jobId=job_id,
                failureDetails={
                    "type": "JobFailed",
                    # The API allows 1-5000 characters; an empty message fails
                    # client-side validation, which would leave the job unanswered.
                    "message": str(exc)[:5000] or exc.__class__.__name__,
                },
            )
    except Exception:
        logger.exception("Could not report job result for %s", job_id)
        return False
    return True


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

    # Drift is judged by comparing source action names. If AFT ever renames or
    # adds one, every pipeline would silently look permanently drifted and all of
    # them would be started daily - so fail loudly on a mismatch instead.
    expected = set(source_actions())
    if not head_is_complete(head, expected):
        raise RuntimeError(
            f"Probe pipeline {probe_pipeline} resolved source actions {sorted(head)}, "
            f"but this module is configured for {sorted(expected)}. AFT's source action "
            "names have changed; update the module before drift can be judged."
        )
    logger.info("HEAD revisions: %s", {k: short(v) for k, v in head.items()})

    pipelines = list_aft_pipelines(codepipeline, pattern)
    logger.info("Found %d AFT customizations pipeline(s)", len(pipelines))

    drifted, skipped, failing = [], [], []
    for name in pipelines:
        status = pipeline_status(codepipeline, name, head)
        if not status["drifted"]:
            continue
        if status["active"]:
            skipped.append(status)
            continue
        if status["failed_on_head"]:
            # Its newest attempt already ran HEAD and failed. Restarting it every
            # day would just repeat the failure, so report it and move on.
            failing.append(status)
            continue
        drifted.append(status)

    started, deferred, failed_to_start = start_pipelines(codepipeline, drifted, max_runs, dry_run)

    summary = {
        "probe_pipeline": probe_pipeline,
        "head_revisions": head,
        "pipelines_checked": len(pipelines),
        "drifted": [s["pipeline"] for s in drifted],
        "started": [s["pipeline"] for s in started],
        "skipped_already_running": [s["pipeline"] for s in skipped],
        "failing_on_head": [s["pipeline"] for s in failing],
        "deferred_over_limit": [s["pipeline"] for s in deferred],
        "failed_to_start": [s["pipeline"] for s in failed_to_start],
        "dry_run": dry_run,
    }
    logger.info("Drift check summary: %s", json.dumps(summary, default=str))

    if env_flag("NOTIFY_ON_DRIFT", True) and (drifted or failing or failed_to_start):
        # Notification failure must not invert a run whose starts all succeeded.
        try:
            publish(
                sns,
                topic_arn,
                _subject(summary, drifted, failing),
                _format_message(summary, head, drifted, skipped, deferred, failing, dry_run),
            )
        except Exception:
            logger.exception("Could not publish the drift check summary")
    return summary


def _subject(summary: dict, drifted: list, failing: list) -> str:
    parts = []
    if drifted:
        parts.append(f"{len(drifted)} behind HEAD")
    if failing:
        parts.append(f"{len(failing)} failing on HEAD")
    if summary["failed_to_start"]:
        parts.append(f"{len(summary['failed_to_start'])} could not be started")
    return f"AFT drift check: {', '.join(parts)}"


def _format_message(summary, head, drifted, skipped, deferred, failing, dry_run) -> str:
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
    if failing:
        lines += [
            "",
            "Not started - newest execution already ran HEAD and did not succeed:",
            *[f"  {s['pipeline']} [{s['status']}]" for s in failing],
        ]
    if summary["failed_to_start"]:
        lines += [
            "",
            "Could not be started:",
            *[f"  {s}" for s in summary["failed_to_start"]],
        ]
    if deferred:
        lines += [
            "",
            "Deferred to the next run (MAX_PIPELINES_PER_RUN reached):",
            *[f"  {s['pipeline']}" for s in deferred],
        ]
    return "\n".join(lines)
