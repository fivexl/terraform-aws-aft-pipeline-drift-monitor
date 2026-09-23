"""Re-apply the AFT customizations to every account, drift or not.

Runs on its own weekly schedule. Where the drift detector is surgical - it only
re-runs accounts whose last successful commit is behind HEAD - this is the
periodic baseline apply: it re-applies the customizations to every account, so
manual console changes, out-of-band edits and anything else that has drifted in
the *account* (rather than in the repository) gets corrected too.

It does that by handing AFT's own ``aft-invoke-customizations`` state machine a
single ``{"type": "all"}`` selector with
``bypass_steps = ["provisioning_bootstrap"]``, which means this Lambda inspects
nothing at all:

* **"every account" is AFT's answer, not ours.** ``all`` resolves against AFT's
  account metadata table, so it covers every AFT-managed account - including one
  whose pipeline this module's ``pipeline_name_pattern`` would not have matched,
  and excluding one whose pipeline exists but which AFT no longer manages.
* **Concurrency is AFT's budget.** The state machine loops on
  ``maximum_concurrent_customizations``, starting what fits and waiting 30s for
  the rest, so there is no cap to configure here and no account left for a later
  week.
* **Already-running pipelines are AFT's problem too.** Its
  ``Get Pipeline Executions`` state counts live executions before starting any,
  which is what this Lambda used to approximate by reading ten executions of
  every pipeline itself.

One ``StartExecution`` per week, with a deterministic name derived from the
EventBridge event id, so a duplicate delivery of the same scheduled event is a
no-op rather than a second estate-wide apply.
"""

from __future__ import annotations

import json
import logging
import os
import uuid

import boto3

from aft_pipelines import (
    configure_logging,
    customizations_input,
    env_flag,
    execution_name,
    invoke_customizations,
    publish,
)

logger = logging.getLogger(__name__)
configure_logging()

stepfunctions = boto3.client("stepfunctions")
sns = boto3.client("sns")


def lambda_handler(event, context):  # noqa: ARG001 - Lambda signature
    """Entry point. Hands every AFT account to the customizations state machine."""
    # EventBridge stamps each scheduled event with a stable id, and Lambda may
    # deliver the same event more than once. Carrying that id into the execution
    # name is what makes a duplicate delivery idempotent - the old handler
    # discarded it.
    summary = run_all((event or {}).get("id"))
    logger.info("Full run summary: %s", json.dumps(summary, default=str))
    if env_flag("NOTIFY_WHEN_CLEAN") or _is_actionable(summary):
        # A notification failure must not mask a run that invoked AFT.
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

    A fresh invocation is worth reporting: it names the execution someone can
    follow. The one non-event is a duplicate delivery whose execution already
    existed - nothing happened, by design. A dry run and a failed invocation are
    always actionable.
    """
    return bool(summary["dry_run"] or summary["invoke_error"] or not summary["already_invoked"])


def run_all(event_id: str | None = None) -> dict:
    """Invoke the customizations state machine for every AFT-managed account."""
    state_machine_arn = os.environ["AFT_INVOKE_STATE_MACHINE_ARN"]
    name_prefix = os.environ.get("NAME_PREFIX", "aft-full-run")
    dry_run = env_flag("DRY_RUN")

    payload = customizations_input()
    key = event_id or f"manual-{uuid.uuid4().hex[:12]}"
    if not event_id:
        logger.info("No EventBridge event id; using one-off invocation key %s", key)
    name = execution_name(name_prefix, key, payload)

    if dry_run:
        logger.info("DRY_RUN: would invoke %s as %s with %s", state_machine_arn, name, payload)
        return {
            "scope": "all AFT-managed accounts",
            "execution_name": name,
            "execution_arn": None,
            "already_invoked": False,
            "invoke_error": None,
            "dry_run": True,
        }

    invoke_error = None
    invocation: dict = {"execution_name": name, "execution_arn": None, "already_invoked": False}
    try:
        invocation = invoke_customizations(stepfunctions, state_machine_arn, payload, name)
    except Exception as exc:
        logger.exception("Could not invoke %s", state_machine_arn)
        invoke_error = str(exc)

    return {
        "scope": "all AFT-managed accounts",
        **invocation,
        "invoke_error": invoke_error,
        "dry_run": False,
    }


def _subject(summary: dict) -> str:
    if summary["invoke_error"]:
        return "AFT weekly full run: could not invoke aft-invoke-customizations"
    if summary["dry_run"]:
        return "AFT weekly full run: would re-apply every AFT-managed account"
    if summary["already_invoked"]:
        return "AFT weekly full run: already invoked, nothing to do"
    return "AFT weekly full run: re-applying every AFT-managed account"


def _format_message(summary: dict) -> str:
    lines = [
        "AFT customizations weekly full run - every account, drift or not.",
        "",
        "Handed to AFT's aft-invoke-customizations state machine as a single",
        'include: [{"type": "all"}] selector with',
        'bypass_steps: ["provisioning_bootstrap"], so AFT resolves the account',
        "list from its own metadata table and starts them within its",
        "maximum_concurrent_customizations budget, waiting for the rest.",
        "",
        f"Execution name: {summary['execution_name']}",
    ]
    if summary["execution_arn"]:
        lines.append(f"Execution ARN:  {summary['execution_arn']}")
    if summary["invoke_error"]:
        lines += [
            "",
            "The state machine could not be invoked, so NOTHING was re-applied:",
            f"  {summary['invoke_error']}",
        ]
    elif summary["already_invoked"]:
        lines += [
            "",
            "That execution already existed, so this was a duplicate delivery of the",
            "same scheduled event and nothing was started twice.",
        ]
    if summary["dry_run"]:
        lines += ["", "DRY_RUN is set: AFT was not invoked."]
    return "\n".join(lines)
