# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `notify_status_report_when_clean` variable (default `false`) gating the
  scheduled `status-report` Lambda's SNS publish: an all-current report with a
  fully-resolved HEAD is now suppressed by default, so only an actionable
  status report (a failure, drift, an unresolved/partial HEAD, or no matching
  pipeline) is sent. Set it to `true` to keep the prior behaviour of a report
  on every scheduled run.
- `lambda_ignore_source_code_hash` variable (default `true`), passed through
  to all three `terraform-aws-modules/lambda/aws` functions. Suppresses a
  spurious plan/apply mismatch that can require a second `apply` on the first
  run in a fresh working directory, caused by the module computing
  `source_code_hash` from a packaged archive that does not exist yet at that
  point. Real code changes still deploy regardless of this setting: this
  module derives each function's `aws_lambda_function.filename` from the
  content of `src/` on every plan, and that filename changing - not
  `source_code_hash` - is what the AWS provider actually redeploys on.
- `notify_full_run_when_clean` variable (default `false`), the same gate as
  `notify_status_report_when_clean` applied to the weekly `full-run` Lambda:
  when nothing was started and nothing failed to start (every pipeline was
  already running, or there was simply nothing eligible), the SNS summary is
  no longer published. A dry run, a failed start, or no matching pipeline
  still always publishes.

## [1.0.0] - 2026-08-20

### Added

- Revision probe CodePipeline that resolves HEAD of the AFT global and account
  customizations repositories through the existing AFT CodeConnections
  connection, on a daily schedule and (optionally) on every push.
- `drift-detector` Lambda that compares every `<account-id>-customizations-pipeline`
  against HEAD and starts the ones whose last successful execution is behind,
  bounded by `max_pipelines_per_run` and skippable with `dry_run`.
- EventBridge rule forwarding AFT customizations pipeline failures straight to
  SNS with an input transformer.
- `status-report` Lambda publishing failures, still-running and still-drifted
  pipelines to the same SNS topic on its own schedule.
- Optional module-managed SNS topic via `create_sns_topic` (default `true`), or
  use an existing one via `sns_topic_arn`, which always takes precedence.
- `full-run` Lambda on a weekly schedule (`full_run_schedule_expression`,
  default Monday 06:00 UTC) that starts every AFT customizations pipeline
  regardless of drift, skipping executions already in flight and selecting
  oldest-execution-first so a capped run rotates through every account. Corrects
  drift inside an account, which no commit comparison can detect.
- Customer-managed KMS key (or bring your own with `kms_key_arn`) encrypting both
  the SNS topic and the probe pipeline's artifacts. A CMK is required, not a
  preference: EventBridge cannot publish to a topic encrypted with the
  AWS-managed `alias/aws/sns` key.
- Versioned S3 artifact bucket for the probe pipeline, with TLS-only access, all
  public access blocked, and objects expiring after `artifact_retention_days`.
- `detect_changes` (default `true`) so the probe pipeline also runs on every push
  to a customizations repository, not only on the daily schedule.
- `notify_on_drift` (default `true`) to control the per-check SNS summary.
- The pipeline-failure EventBridge target publishes through an IAM role
  (`pipeline_failed_target_role_arn`), so `sns_topic_arn` may name a topic in
  another account. The roleless path authenticates as the
  `events.amazonaws.com` service principal, which AWS only accepts for a topic
  in the same account as the rule.

- Optional Slack delivery through Amazon Q Developer in chat applications
  (`enable_chatbot`), using the AWS provider's native
  `aws_chatbot_slack_channel_configuration` so no extra provider is required.
  Creates a read-only role for Chatbot and applies `ReadOnlyAccess` as the
  channel guardrail rather than AWS's `AdministratorAccess` default.

### Notes

- Requires Terraform **>= 1.9.0**: the `create_sns_topic` validation references
  another variable, which earlier versions do not allow.

[Unreleased]: https://github.com/fivexl/terraform-aws-aft-pipeline-drift-monitor/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/fivexl/terraform-aws-aft-pipeline-drift-monitor/releases/tag/v1.0.0
