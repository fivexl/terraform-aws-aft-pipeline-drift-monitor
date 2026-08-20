# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

### Notes

- Requires Terraform **>= 1.9.0**: the `create_sns_topic` validation references
  another variable, which earlier versions do not allow.

[Unreleased]: https://github.com/fivexl/terraform-aws-aft-pipeline-drift-monitor/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/fivexl/terraform-aws-aft-pipeline-drift-monitor/releases/tag/v1.0.0
