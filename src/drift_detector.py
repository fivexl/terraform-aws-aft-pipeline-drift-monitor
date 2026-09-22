"""Detect AFT customizations pipelines running stale commits and re-run them.

Invoked as a CodePipeline ``Lambda`` action from the revision probe pipeline
this module creates: the probe's source stage resolves HEAD of both
customizations repositories through the existing CodeConnections connection and
passes them on as input artifacts, whose ``revision`` fields carry the resolved
commit ids straight to this handler.

Every AFT pipeline whose last successful execution used an older commit is
re-run by handing its **account id** to AFT's own ``aft-invoke-customizations``
state machine, in one execution, with ``bypass_steps = ["provisioning_bootstrap"]``
(unless ``DRY_RUN`` is set). This module therefore decides *which* accounts are
behind and AFT decides *when* each one runs - its state machine holds the
concurrency budget and the completion controls, neither of which a direct
``StartPipelineExecution`` passes through.

One consequence worth knowing: ``StartExecution`` returns an execution ARN, not a
per-account result, and AFT filters out any account missing from its own metadata
table. So this Lambda cannot confirm that a given account was re-run - it
confirms only that the accounts were accepted. Verifying they actually caught up
is the scheduled status report's job, which compares applied revisions against
HEAD on its own schedule.

Pipeline failures are reported to SNS by an EventBridge rule, not from here.
"""

from __future__ import annotations

import json
import logging
import os
import uuid

import boto3

from aft_pipelines import (
    DEFAULT_PIPELINE_PATTERN,
    account_id_from_pipeline,
    configure_logging,
    customizations_input,
    env_flag,
    execution_name,
    head_is_complete,
    head_revisions,
    inspect_pipelines,
    invoke_customizations,
    list_aft_pipelines,
    publish,
    revisions_from_job_artifacts,
    short,
    source_actions,
    validate_source_actions,
)

logger = logging.getLogger(__name__)
configure_logging()

codepipeline = boto3.client("codepipeline")
stepfunctions = boto3.client("stepfunctions")
sns = boto3.client("sns")


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
    """Entry point. Reports job success/failure when driven by CodePipeline."""
    job = (event or {}).get("CodePipeline.job") or {}
    job_id = job.get("id")
    try:
        result = detect_and_run(job)
    except Exception as exc:  # must not leave the CodePipeline job hanging
        logger.exception("Drift detection failed")
        if job_id and _report_job(job_id, str(exc)[:5000] or exc.__class__.__name__):
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

    # Every account was processed, but something required did not happen: the
    # job must not be reported as successful, or the probe pipeline goes green
    # and the EventBridge failure rule never fires on a partial remediation.
    failure = _degraded_failure(result)
    if job_id and not _report_job(job_id, failure):
        # The work is done, but CodePipeline was never told. The action now hangs
        # until its own timeout, which no EventBridge failure rule reports in the
        # meantime - so alert directly. Best effort: the result still stands.
        try:
            publish(
                sns,
                os.environ.get("SNS_TOPIC_ARN", ""),
                "AFT drift check completed with failures but CodePipeline was not notified"
                if failure
                else "AFT drift check succeeded but CodePipeline was not notified",
                "The AFT pipeline drift check completed"
                + (f" with failures ({failure})" if failure else " successfully")
                + f", but reporting the result for CodePipeline job {job_id} failed.\n\n"
                "The probe pipeline's Detect-Drift action is now unanswered and will "
                "hang until its action timeout expires. Any drifted pipelines were "
                "already started, so no drift check needs to be repeated - see the "
                "Lambda logs for the underlying PutJob*Result error.",
            )
        except Exception:
            logger.exception("Could not publish the unreported job-result alert")
    return result


def _degraded_failure(summary: dict) -> str | None:
    """One-line reason the job must fail, or None when the run was clean.

    Recording a failure and then answering the job with ``PutJobSuccessResult``
    makes the probe pipeline go green, which is the only thing the EventBridge
    failure rule watches - so a pipeline that could not be inspected, a
    quarantined pipeline, and a state machine invocation that never landed would
    all be visible in the logs alone. Every account is still processed first;
    only the verdict changes.

    Note what is deliberately NOT here: whether each account actually re-ran.
    ``StartExecution`` returns one execution ARN for the whole batch, and AFT
    drops any account missing from its metadata table, so this Lambda cannot know
    that. The status report is what notices an account that stayed behind HEAD.
    """
    reasons = [
        f"{len(summary[key])} pipeline(s) {label}"
        for key, label in (
            ("inspect_errors", "could not be inspected"),
            ("quarantined", "quarantined as incompatible"),
            ("unresolved_accounts", "have no account id in their name"),
        )
        if summary[key]
    ]
    if summary["invoke_error"]:
        reasons.append(f"the customizations state machine could not be invoked: {summary['invoke_error']}")
    return ", ".join(reasons) or None


def _report_job(job_id: str, failure: str | None = None) -> bool:
    """Answer the CodePipeline job. Returns False if the answer could not be sent."""
    try:
        if failure is None:
            codepipeline.put_job_success_result(jobId=job_id)
        else:
            codepipeline.put_job_failure_result(
                jobId=job_id,
                failureDetails={
                    "type": "JobFailed",
                    # The API allows 1-5000 characters; an empty message fails
                    # client-side validation, which would leave the job unanswered.
                    "message": failure[:5000] or "JobFailed",
                },
            )
    except Exception:
        logger.exception("Could not report job result for %s", job_id)
        return False
    return True


def detect_and_run(job: dict) -> dict:
    """Compare every AFT pipeline against HEAD and re-run the stale ones."""
    probe_pipeline = os.environ["PROBE_PIPELINE_NAME"]
    pattern = os.environ.get("PIPELINE_NAME_PATTERN", DEFAULT_PIPELINE_PATTERN)
    topic_arn = os.environ.get("SNS_TOPIC_ARN", "")
    state_machine_arn = os.environ["AFT_INVOKE_STATE_MACHINE_ARN"]
    name_prefix = os.environ.get("NAME_PREFIX", "aft-drift")
    dry_run = env_flag("DRY_RUN")

    # HEAD comes from the job event's input artifacts: CodePipeline stamps each
    # one with the commit it resolved for this very execution. The event carries
    # no pipelineContext (AWS omits it for Lambda actions), so there is no
    # execution id to look up - and no need for one. head_revisions() remains the
    # path for direct invocation, which has no job and therefore no artifacts.
    head = revisions_from_job_artifacts(job)
    if not head:
        if job:
            logger.warning(
                "CodePipeline job carried no input artifact revisions; falling back to the "
                "most recent %s execution. Check that the Detect-Drift action has "
                "input_artifacts wired.",
                probe_pipeline,
            )
        head = head_revisions(codepipeline, probe_pipeline)
    if not head:
        raise RuntimeError(
            f"Could not resolve HEAD revisions from probe pipeline {probe_pipeline}. "
            "Check the CodeConnections connection and the probe pipeline's source stage."
        )

    logger.info("HEAD revisions: %s", {k: short(v) for k, v in head.items()})

    tracked = set(source_actions())
    if not head_is_complete(head, tracked):
        # A probe execution inspected mid-flight can carry only one of the two
        # source revisions. drifted_against() iterates over HEAD's keys, so a
        # missing action is simply not compared: every account looks current on
        # it. That is a silent false negative across the whole estate, and it
        # reports success while leaving accounts behind HEAD - strictly worse
        # than resolving no HEAD at all, which at least fails loudly.
        raise RuntimeError(
            f"HEAD from probe pipeline {probe_pipeline} resolved "
            f"{sorted(head)} but this module tracks {sorted(tracked)}. Drift "
            "cannot be judged against a partial HEAD: the missing source "
            "action would be treated as current on every account. Check that "
            "the Detect-Drift action has every source as an input artifact, "
            "and that the probe pipeline's source stage completed."
        )

    pipelines = list_aft_pipelines(codepipeline, pattern)
    logger.info("Found %d AFT customizations pipeline(s)", len(pipelines))

    # Quarantine before inspecting: a pipeline whose source actions are not the
    # ones drift is keyed on cannot be compared against HEAD at all, and must be
    # reported rather than either blocking the estate or being judged wrongly.
    quarantined = validate_source_actions(codepipeline, pipelines, tracked)
    if quarantined:
        for name, reason in sorted(quarantined.items()):
            logger.error("Quarantined %s: %s", name, reason)

    statuses, inspect_errors = inspect_pipelines(
        codepipeline, [name for name in pipelines if name not in quarantined], head
    )

    drifted, skipped, failing = [], [], []
    for status in statuses:
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

    # The state machine selects ACCOUNTS, not pipelines. AFT names every
    # customizations pipeline after its account, so the target is readable from
    # the name; a pipeline whose name carries no account id cannot be re-run this
    # way and is reported rather than silently dropped.
    accounts, unresolved = [], []
    for status in drifted:
        account = account_id_from_pipeline(status["pipeline"])
        if account:
            accounts.append(account)
        else:
            unresolved.append(status["pipeline"])

    invocation: dict = {}
    invoke_error = None
    if accounts and dry_run:
        logger.info("DRY_RUN: would invoke %s for %s", state_machine_arn, sorted(accounts))
    elif accounts:
        payload = customizations_input(accounts)
        # A CodePipeline job id is stable across a duplicate Lambda delivery of
        # the same invocation, which is what makes the execution name - and so
        # the whole re-run - idempotent. Direct invocation has no such id and
        # gets a fresh one: a human running this by hand means it.
        key = job.get("id") or f"manual-{uuid.uuid4().hex[:12]}"
        if not job.get("id"):
            logger.info("No CodePipeline job id; using one-off invocation key %s", key)
        try:
            invocation = invoke_customizations(
                stepfunctions,
                state_machine_arn,
                payload,
                execution_name(name_prefix, key, payload),
            )
        except Exception as exc:  # reported as a degraded run, not an abort
            logger.exception("Could not invoke %s", state_machine_arn)
            invoke_error = str(exc)

    summary = {
        "probe_pipeline": probe_pipeline,
        "head_revisions": head,
        "pipelines_checked": len(statuses),
        "drifted": [s["pipeline"] for s in drifted],
        "invoked_accounts": sorted(accounts),
        "skipped_already_running": [s["pipeline"] for s in skipped],
        "failing_on_head": [s["pipeline"] for s in failing],
        "unresolved_accounts": unresolved,
        "inspect_errors": inspect_errors,
        "quarantined": quarantined,
        "execution_arn": invocation.get("execution_arn"),
        "already_invoked": bool(invocation.get("already_invoked")),
        "invoke_error": invoke_error,
        "dry_run": dry_run,
    }
    logger.info("Drift check summary: %s", json.dumps(summary, default=str))

    degraded = _degraded_failure(summary)
    # ``skipped`` is included because notify_on_drift's contract promises a
    # summary for skipped pipelines. A degraded run publishes regardless of the
    # flag: notify_on_drift governs an informational drift summary, and must not
    # be able to hide an operational failure.
    if degraded or (env_flag("NOTIFY_ON_DRIFT", True) and (drifted or failing or skipped)):
        # Notification failure must not invert a run whose invocation succeeded.
        try:
            publish(
                sns,
                topic_arn,
                _subject(summary, drifted, failing, skipped),
                _format_message(summary, head, drifted, skipped, failing, dry_run),
            )
        except Exception:
            logger.exception("Could not publish the drift check summary")
    return summary


def _subject(summary: dict, drifted: list, failing: list, skipped: list) -> str:
    parts = []
    if drifted:
        parts.append(f"{len(drifted)} behind HEAD")
    if failing:
        parts.append(f"{len(failing)} failing on HEAD")
    if skipped:
        parts.append(f"{len(skipped)} already running")
    if summary["unresolved_accounts"]:
        parts.append(f"{len(summary['unresolved_accounts'])} without an account id")
    if summary["inspect_errors"]:
        parts.append(f"{len(summary['inspect_errors'])} could not be inspected")
    if summary["quarantined"]:
        parts.append(f"{len(summary['quarantined'])} quarantined")
    if summary["invoke_error"]:
        parts.append("could not invoke AFT")
    return f"AFT drift check: {', '.join(parts) or 'nothing to report'}"


def _format_message(summary, head, drifted, skipped, failing, dry_run) -> str:
    lines = [
        "AFT customizations pipeline drift check",
        "",
        "HEAD revisions:",
        *[f"  {action}: {short(revision)}" for action, revision in sorted(head.items())],
        "",
        f"Pipelines checked: {summary['pipelines_checked']}",
        f"Behind HEAD:       {len(drifted)}",
        f"{'Would re-run' if dry_run else 'Handed to AFT'}:     "
        f"{len(summary['invoked_accounts'])} account(s)",
        "",
    ]
    if summary["execution_arn"]:
        lines += [
            (
                "Already invoked by an earlier delivery of this check: "
                if summary["already_invoked"]
                else "aft-invoke-customizations execution: "
            )
            + summary["execution_arn"],
            "",
            "AFT's state machine starts these accounts within its own",
            "maximum_concurrent_customizations budget and waits for the rest, so no",
            "account is deferred to a later check.",
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
            "Not re-run - newest execution already ran HEAD and failed:",
            *[f"  {s['pipeline']} [{s['status']}]" for s in failing],
        ]
    if summary["unresolved_accounts"]:
        lines += [
            "",
            "No account id in the pipeline name, so not re-runnable through AFT's "
            "state machine (check pipeline_name_pattern):",
            *[f"  {s}" for s in summary["unresolved_accounts"]],
        ]
    if summary["inspect_errors"]:
        lines += [
            "",
            "Could not be inspected - not judged this run, will be retried:",
            *[f"  {e['pipeline']}: {e['error']}" for e in summary["inspect_errors"]],
        ]
    if summary["quarantined"]:
        lines += [
            "",
            "Quarantined - source actions do not match the ones drift is judged on, "
            "so these were neither compared nor re-run:",
            *[f"  {name}: {reason}" for name, reason in sorted(summary["quarantined"].items())],
        ]
    if summary["invoke_error"]:
        lines += [
            "",
            "The aft-invoke-customizations state machine could not be invoked, so "
            "NOTHING was re-run this check:",
            f"  {summary['invoke_error']}",
        ]
    if dry_run:
        lines += ["", "DRY_RUN is set: AFT was not invoked."]
    return "\n".join(lines)
