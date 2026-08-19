# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-08-18

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
  regardless of drift, skipping executions already in flight. Corrects drift
  inside an account, which no commit comparison can detect.

[Unreleased]: https://github.com/fivexl/terraform-aws-aft-pipeline-drift-monitor/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/fivexl/terraform-aws-aft-pipeline-drift-monitor/releases/tag/v1.0.0
