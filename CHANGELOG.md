# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.0] - 2026-09-23

### Added

- A plan-time AFT version floor. Terraform reads `/aft/config/aft/version` and
  fails with an explicit message on AFT < 1.21.0, because `bypass_steps` is a
  1.21.0 feature and an older state machine would silently fall through to
  `Invoke Provisioning Framework` - running the full provisioning framework for
  every targeted account instead of a customizations re-run. There is
  deliberately no runtime fallback. The check is fail-open on a version string it
  cannot parse.
- Unresolvable-account reporting: a discovered pipeline whose name carries no
  account id cannot be re-run through the state machine, so it is named in the
  summary and fails the check rather than being skipped silently.
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
- `notification_publisher_role_arns` output: a named map of every principal this
  module publishes to the notification topic with - the three Lambda execution
  roles and the EventBridge failure-alert role. `sns_topic_arn`'s own description
  tells a cross-account topic owner to authorize exactly these, but only the
  EventBridge role was output, so the rest had to be guessed from generated role
  names, granted at account scope, or discovered as a runtime `AccessDenied`.
- `kms_key_arn` output, so a cross-account topic owner can see which key the
  publishers decrypt with.
- `artifact_access_log_bucket` / `artifact_access_log_prefix`: opt-in S3 server
  access logging for the probe pipeline's artifact bucket. Off by default because
  logging a bucket requires a second permanent bucket this module should not
  create for you; worth enabling where AFT's S3 data events are disabled and
  reads of the customization source archives would otherwise be recorded nowhere.
- `cloudwatch_logs_kms_key_id`: encrypts the three Lambda log groups with a
  customer-managed key. Deliberately separate from `kms_key_arn`, which cannot be
  reused - a CloudWatch Logs key needs a different key policy.
- `.github/dependabot.yml` covering github-actions, Terraform (root and the
  example) and pip. Terraform modules, Python tooling and Action upgrades were
  entirely manual, and the SHA-pinned reusable workflows below are only
  maintainable with a bot watching them.
- `.tflint-aws.hcl` plus TFLint, Trivy and gitleaks pre-commit hooks, and a
  `tflint` CI job that runs `tflint --init` first. TFLint's AWS ruleset validates
  ARN shapes and deprecated arguments against the real API, which
  `terraform validate` does not look at. The config is deliberately *not* named
  `.tflint.hcl`: the reusable workflow runs a bare `tflint` with no
  `tflint --init`, so an auto-discovered config declaring an external plugin fails
  with `Plugin 'aws' not found`. Both callers that should load it - the pre-commit
  hook and the CI job - name it explicitly.

### Changed

- **Customizations are re-run through AFT's own `aft-invoke-customizations`
  state machine** instead of by calling `codepipeline:StartPipelineExecution`
  directly. Both Lambdas now issue one `states:StartExecution` with
  `bypass_steps = ["provisioning_bootstrap"]` - the drift check passing the
  drifted account ids, the weekly full run passing `[{"type": "all"}]`. Direct
  starts bypassed AFT's concurrency and completion controls, so a widespread
  drift event could launch more Terraform than the AFT installation was
  configured to handle. The state machine is a backpressure loop rather than a
  cap: it starts what fits inside AFT's own
  `maximum_concurrent_customizations` and waits 30s for the rest, indefinitely.
- **Re-runs are idempotent.** The Step Functions execution name is derived from
  the invocation's own id - the CodePipeline job id for the drift check, the
  EventBridge event id for the weekly run, which was previously discarded - plus
  a digest of the account set. A duplicate asynchronous delivery therefore hits
  `ExecutionAlreadyExists` and starts nothing, while a retry that resolved a
  different set of accounts is correctly treated as new work. No
  `clientRequestToken` is needed.
- **The weekly full run inspects nothing.** It no longer lists pipelines or reads
  ten executions of each; `include: [{"type": "all"}]` makes "every account"
  AFT's answer from its metadata table, which covers an account whose pipeline
  `pipeline_name_pattern` would have missed and skips a pipeline AFT no longer
  manages. Its IAM role has lost all CodePipeline permissions and
  `full_run_timeout` now defaults to 60 seconds instead of 600.
- Both Lambda roles trade `codepipeline:StartPipelineExecution` for
  `states:StartExecution` on the `aft-invoke-customizations` state machine ARN,
  scoped to the current region rather than every region.
- The drift check's summary and SNS message now report the accounts handed to AFT
  and the resulting execution ARN, in place of the pipelines started and deferred.
- **Terraform floor lowered from 1.9.0 to 1.6.1.** 1.9 was required solely
  because two variable validations referenced other variables - `create_sns_topic`
  reading `sns_topic_arn`, and `enable_chatbot` reading the two Slack ids. Both
  are now resource preconditions, available since Terraform 1.2, which produce
  the same plan-time failure. 1.6.1 is the floor of the management-AFT stacks
  that consume this module, and CI validates on exactly that version.
- IAM pipeline ARNs are scoped to the provider's region instead of every region.
  All clients and resources operate in the AFT home region the module is deployed
  into, so the wildcard only widened the grant to matching pipelines in every
  other region of the account.
- The reusable workflows in `base.yml` are pinned to a commit SHA rather than
  `@main`, and the workflow declares `permissions: contents: read` at the top
  level. A mutable ref meant CI could change under a PR that did not touch it,
  while running with this repository's token. Note the upstream `1.0.0` tag is
  *older* than the pinned commit, so pinning to the tag would have been an
  unverified behaviour change rather than a stabilisation.
- CI's Terraform security scan is **Trivy instead of tfsec**: the pinned SHA is
  `fivexl/github-reusable-workflows@replace-tfsec-with-trivy`, one commit ahead of
  that repository's `main`, which replaces the deprecated
  `triat/terraform-security-scan` with `aquasecurity/trivy-action`. tfsec's HCL
  parser cannot read Terraform 1.7+ `import` blocks. The check is renamed from
  `terraform-job / TFSec` to `terraform-job / Trivy Security Scan`, so a
  required-status-check rule naming the old one must be updated. This also aligns
  CI with the `terraform_trivy` pre-commit hook added here - the same scanner, the
  same `HIGH,CRITICAL` threshold.
- All three Lambda functions set `publish = false`. EventBridge and CodePipeline
  invoke the unqualified function ARN, so publishing only accumulated immutable
  versions with no alias and no version-qualified rollback path.

### Removed

- **`max_pipelines_per_run` and `full_run_max_pipelines_per_run`.** AFT's state
  machine owns the concurrency budget, so a cap here could only be wrong: it
  cannot see live executions, and slicing the candidate list before attempting
  anything meant failed starts consumed the budget while healthy pipelines were
  deferred (review item 5). Nothing is deferred to a later run any more, which
  also removes the "a 50-account estate takes about three weeks per sweep"
  caveat, and the oldest-execution-first rotation that existed only to work
  around it.

### Fixed

- The drift detector now requires a **complete** HEAD before judging anything.
  It previously rejected only an *empty* revision map, and drift comparison
  iterates over the actions present in HEAD - so a probe execution that resolved
  one of the two sources made every account look current on the missing one, and
  the check reported success while leaving accounts behind HEAD. It now fails the
  CodePipeline job instead, using the `head_is_complete` helper the status report
  already used.
- Source action names are validated on **every** AFT pipeline, not just the first
  one. Sampling `pipelines[0]` meant one hand-edited or partially-upgraded
  pipeline blocked remediation for the whole estate, while an incompatible
  *later* pipeline was never validated and could be judged wrongly. An
  incompatible pipeline is now quarantined and reported - neither compared nor
  started - and every compatible pipeline is still remediated. A pipeline whose
  definition cannot be read is quarantined too, rather than assumed compatible.
- Pipeline inspection is isolated per pipeline in all three Lambdas. A deletion
  race, a throttled API call or a malformed pipeline aborted the entire
  estate-level run; the failure is now recorded against its own pipeline, the
  rest are still processed, and the affected names are reported.
- A run that did not fully remediate is no longer reported to CodePipeline as
  successful. An unstartable pipeline, a pipeline that could not be inspected,
  and a quarantined pipeline now fail the drift detector's job after every other
  account has been processed - so the probe pipeline goes `FAILED` and the
  EventBridge failure rule fires. `notify_on_drift` no longer suppresses these:
  it gates the informational drift summary, not operational failures.
- `notify_on_drift` now publishes for pipelines **skipped because a run is
  already in flight**, which its own documentation promised. The scheduled status
  report likewise treats drift on a still-running pipeline as actionable. Between
  them, a stale pipeline stuck `InProgress` used to appear in neither.
- `failed_on_head` is judged on a `Failed` newest execution only. It previously
  treated every non-`Succeeded` terminal state as "already failed on HEAD",
  including `Superseded`, `Stopped` and `Cancelled` - none of which is evidence
  that HEAD cannot be applied, so a retry that would have fixed the account was
  suppressed.
- The weekly full run reports the number of pipelines it actually **started**.
  The count was derived as eligible minus deferred, which is the number
  *attempted*: three successes and one failure were reported as `started 4`. Dry
  runs keep selected-count semantics, and the subject now carries a failure
  count.
- `failure_pipeline_name_suffix` is validated, and checked for consistency with
  `pipeline_name_pattern` by a precondition that asserts the pattern matches the
  name AFT would give a pipeline carrying that suffix. An empty suffix was
  previously accepted and widened the Lambdas' IAM resource ARN - and the
  EventBridge failure match - to every CodePipeline in the account.

### Notes

- Requires **AFT >= 1.21.0**. On top of
  `/aft/config/vcs/codeconnections-connection-arn`, the module now invokes
  `aft-invoke-customizations` with `bypass_steps`, which is 1.21.0+. Terraform
  reads `/aft/config/aft/version` and fails the plan below that floor.
- Requires Terraform **>= 1.6.1**, down from 1.9.0, verified by CI validating on
  exactly that version.
- This is the **first published version**. `1.0.0` below was developed but never
  tagged or pushed to the Terraform Registry, so the two variables removed above
  were never part of a released interface and no consumer can be pinned to them -
  which is why their removal ships as a minor bump rather than a major one.

## [1.0.0] - 2026-08-20

Never tagged; superseded by `1.1.0` before release. Kept as the record of what
the module looked like when it was first reviewed.

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

- Requires **AFT >= 1.21.0**. The module reads
  `/aft/config/vcs/codeconnections-connection-arn`, which AFT introduced in
  1.13.4 when it migrated from CodeStar Connections to CodeConnections, and
  1.21.0 is the floor this module is supported against.
- Requires Terraform **>= 1.9.0**: the `create_sns_topic` and `enable_chatbot`
  validations reference other variables, which earlier versions do not allow.

[Unreleased]: https://github.com/fivexl/terraform-aws-aft-pipeline-drift-monitor/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/fivexl/terraform-aws-aft-pipeline-drift-monitor/releases/tag/v1.1.0
