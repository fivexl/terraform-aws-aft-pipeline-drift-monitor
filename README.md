[![FivexL](https://releases.fivexl.io/like-this-repo-banner.png)](https://fivexl.io/#email-subscription)

### Want practical AWS infrastructure insights?

👉 [Subscribe to our newsletter](https://fivexl.io/#email-subscription) to get:

- Real stories from real AWS projects  
- No-nonsense DevOps tactics  
- Cost, security & compliance patterns that actually work  
- Expert guidance from engineers in the field

=========================================================================

# terraform-aws-aft-pipeline-drift-monitor

Keeps [AWS Control Tower Account Factory for Terraform](https://github.com/aws-ia/terraform-aws-control_tower_account_factory)
(AFT) customizations in sync with the repositories that define them.

AFT creates one CodePipeline per vended account, `<account-id>-customizations-pipeline`,
whose source actions are configured with **`DetectChanges = false`**. Nothing
re-runs those pipelines when you push to `aft-global-customizations` or
`aft-account-customizations`: each account keeps whatever commit it last ran
with until something starts it again. In a large organisation the accounts
silently spread across many different commits.

This module finds those accounts every day and re-runs them, re-applies every
account once a week to catch drift that happened inside an account, tells you
when a run fails, and reports the outcome a few hours later.

## What it deploys

```
EventBridge (daily)                push to customizations repo (optional)
        │                                        │
        └──────────────►  revision probe pipeline  ◄────────────────┘
                          │  Source: both customizations repos, via the
                          │          existing AFT CodeConnections connection
                          │          → CodePipeline resolves HEAD for us
                          ▼
                    drift detector Lambda
                          │  compares HEAD against the commit each AFT
                          │  pipeline last applied successfully
                          ├──► states:StartExecution on AFT's own
                          │    aft-invoke-customizations, with the drifted
                          │    ACCOUNT ids and bypass_steps
                          │        │
                          │        └──► AFT starts each account's pipeline
                          │             inside its own concurrency budget
                          └──► SNS: "N accounts behind HEAD, handed to AFT"
                                 │
EventBridge (any *-customizations-pipeline FAILED) ───────────┤
                                                                │
EventBridge (a few hours later) ──► status report Lambda ─────┤
                                                                │
EventBridge (weekly) ──► full run Lambda ──► aft-invoke-  ─────┤
                         include: all         customizations    │
                                                                 ▼
                                                          SNS topic
                                                        ┌──────┴──────┐
                                                        ▼             ▼
                                                emailed subscriber   AWS Chatbot
                                                  (or your own       (enable_chatbot)
                                                   subscription)          │
                                                                          ▼
                                                                    Slack channel
```

Four signals, one SNS topic:

| Signal | Source | When |
|---|---|---|
| Drift summary | `drift-detector` Lambda | Each drift check with something to report: stale, failing-on-HEAD, already-running, quarantined, uninspectable or unstartable pipelines |
| Failure alert | EventBridge → SNS directly, via an IAM role | Any pipeline ending in `failure_pipeline_name_suffix` fails, plus the revision probe itself |
| Status report | `status-report` Lambda | On its own schedule, a few hours after the check |
| Weekly full run | `full-run` Lambda | Weekly, after handing every AFT-managed account to AFT |

All four go to one SNS topic, so one subscription covers the whole module —
email, or Slack via `enable_chatbot` (see below). Both can be attached to the
same topic at once; they are independent delivery paths, not alternatives you
choose between once and for all.

## How a re-run happens

This module decides **which** accounts are behind. AFT decides **when** each one
runs. Nothing here calls `StartPipelineExecution`.

Both Lambdas re-run customizations the way AWS documents for a
[re-invocation](https://docs.aws.amazon.com/controltower/latest/userguide/aft-account-customization-options.html):
one `states:StartExecution` on AFT's own `aft-invoke-customizations` state
machine, in the same account and region as this module.

```json
{
  "include": [{ "type": "accounts", "target_value": ["111122223333", "..."] }],
  "bypass_steps": ["provisioning_bootstrap"]
}
```

The drift check sends the drifted accounts; the weekly full run sends
`[{ "type": "all" }]` instead and lets AFT resolve the list from its own account
metadata table. `include` is capped at four items by AFT's request schema, so the
account list travels as one `accounts` selector rather than one selector each.
`get_execution_id` is deliberately absent even though the schema lists it: the
state machine's first state injects it from `$$.Execution.Name`.

Three things follow from delegating, none of which a direct pipeline start can
reproduce:

**Concurrency is AFT's, and it is a backpressure loop rather than a cap.** The
state machine runs `Get Pipeline Executions → Below Maximum Execution Threshold?
→ Execute Pipelines → Wait 30s → recheck` against AFT's own
`maximum_concurrent_customizations`. It starts what fits and *waits* for the
rest, indefinitely. So this module has no `max_pipelines_per_run` of its own:
there is nothing to tune, nothing is deferred to a later check, and a 50-account
estate is re-applied in one run rather than over three weeks. Direct starts
bypassed that budget entirely and could launch more Terraform than the AFT
installation was configured to handle.

**Re-runs are idempotent without a `clientRequestToken`.** Lambda can deliver an
asynchronous event more than once, including after a successful run. The
execution name is derived from the invocation's own id — the CodePipeline job id
for the drift check, the EventBridge event id for the weekly run — plus a digest
of the account set, so a duplicate delivery hits `ExecutionAlreadyExists` and
starts nothing. A retry that resolved a *different* set of accounts is new work
and is not suppressed. A manual invocation has no stable id and is deliberately
not deduplicated: running it twice by hand means it twice.

**Completion and audit are AFT's.** A re-run lands in AFT's customizations audit
record and its own success/failure notifications, which a direct start skipped.

### What this module can no longer tell you

`StartExecution` returns one execution ARN, not a per-account result, and AFT
drops any account missing from its metadata table before starting anything. So
the drift check confirms that the accounts were **accepted**, not that each one
re-ran. Verifying that they actually caught up is the scheduled status report's
job: it compares applied revisions against HEAD on its own schedule and names
anything still behind. That is a deliberate trade for AFT's orchestration, and
the reason the report matters more than it did.

### Why AFT >= 1.21.0, with no fallback

`bypass_steps` is what makes this a *customizations* re-run rather than a full
re-provision, and AFT introduced it in **1.21.0**. The `Check Bypass` state that
reads it is written in JSONata and cannot be backported.

There is no graceful degradation below that, on purpose. Without `bypass_steps`
the choice state does not exist and every invocation falls through to
`Invoke Provisioning Framework` — a `DISTRIBUTED` Map running the full
provisioning framework for every targeted account. That is far heavier than
anything this module is asking for, and it fails silently, so a fallback would be
worse than a refusal. Terraform therefore reads `/aft/config/aft/version` and
**fails the plan** on an older AFT rather than letting a 02:00 Lambda discover it.
The check is fail-open on a version string it cannot parse: it exists to catch a
genuine 1.20.x install, not to block a format AFT has not used yet.

## Two different kinds of drift

The daily check and the weekly full run answer different questions, which is why
both exist:

- **Repository drift** — the account is applied at an older commit than HEAD. The
  drift detector finds this and re-runs only the affected accounts.
- **Account drift** — the account no longer matches the customizations that were
  applied to it, because somebody changed something in the console. No commit
  comparison can see this. The weekly full run corrects it by re-applying the
  customizations to **every** account whether or not its commit is current.

The full run does not inspect anything: it hands AFT a single
`include: [{"type": "all"}]` selector, so "every account" is AFT's own answer
from its metadata table rather than this module's pipeline-name pattern. That is
more accurate in both directions — it covers an account whose pipeline the
pattern would have missed, and skips a pipeline AFT no longer manages. Skipping
pipelines that are already running is AFT's job too: its `Get Pipeline Executions`
state counts live executions before starting any.

It obeys `dry_run` the same way the drift check does. There is no cap to set on
either: see [How a re-run happens](#how-a-re-run-happens).

## How HEAD is resolved

There is **no CodeConnections API that returns the current commit of a connected
repository** — a connection is an authorisation grant that services use on your
behalf, not a readable Git client. Rather than introduce a second GitHub
credential, this module borrows CodePipeline's own resolution:

1. The *revision probe* pipeline points the existing AFT connection
   (`/aft/config/vcs/codeconnections-connection-arn`) at the same two
   customizations repositories, using the same branches and the same source
   action names as AFT.
2. When it runs, CodePipeline fetches each repository through the connection and
   reports the commit it resolved as the execution's source revision.
3. The probe's second stage invokes the drift detector, passing the execution
   id. That commit is HEAD.

If that execution has not recorded its revisions yet, the detector falls back to
the most recent probe execution that has them (the last five are searched), so
HEAD can briefly be a previous probe run's commit — it logs which execution it
used. The status report always reads the latest probe execution, since it is not
invoked from inside one.

Comparing pipelines against each other instead would not work: right after a
push *every* pipeline is behind, so the newest commit any of them has seen is
still the old one, and nothing would ever be detected.

With `detect_changes = true` (the default) the probe pipeline also runs on every
push to either repository, so drift is corrected within minutes instead of
waiting for the next daily run.

## What counts as drift

A pipeline is behind if its **last successful** execution used a different
commit than HEAD, for either source action. The last successful execution is
what the account is actually running — a later failed or cancelled attempt does
not change that.

Only the **10 most recent executions** of each pipeline are inspected, which
bounds the API calls per check. A pipeline with no success among those 10 counts
as drifted, so a failure is retried on the next check. Two cases are deliberately
*reported but not restarted*:

- an execution is `InProgress` or `Stopping` — the run under way will settle it
- the newest execution **`Failed`** while already carrying HEAD — another run
  would only repeat the same failure, so it is listed as *failing on HEAD*
  instead. `Superseded`, `Stopped` and `Cancelled` do **not** count here: none of
  them is evidence that HEAD cannot be applied, so those pipelines are retried.

Drift is judged by source **action name**, so if AFT renames or adds one, the
affected pipeline can no longer be compared against HEAD at all — its applied
revisions would never match HEAD's keys and it would look permanently behind,
restarting on every run. Every pipeline's source actions are therefore read from
its own definition (`GetPipeline`) on each check, and any pipeline that does not
match exactly is **quarantined**: neither compared nor started, and reported. One
hand-edited pipeline does not block the rest of the estate, and no incompatible
pipeline is silently judged. A pipeline whose definition cannot be read is
quarantined too — an unreadable definition is not evidence that it still matches.

Drift is only judged against a **complete** HEAD. A probe execution inspected
mid-flight can carry one of the two source revisions, and comparison iterates
over the actions HEAD actually has — so a missing action would be treated as
current on every account. The check fails instead.

### When the check reports failure

The drift detector processes every account first and only then decides the
CodePipeline job's verdict. It reports the job as **failed** — so the probe
pipeline goes `FAILED` and the failure alert fires — when any pipeline could not
be started, could not be inspected, or was quarantined. Every other account is
still remediated in the same run; only the verdict changes. `notify_on_drift`
gates the informational drift summary and cannot suppress these.

A pipeline that could not be inspected is excluded from that run's counts and
retried on the next one, rather than costing the whole estate its check.

## Usage

Deploy into the **AFT management account**, in the **AFT home region**.

```hcl
module "aft_pipeline_drift_monitor" {
  source  = "fivexl/aft-pipeline-drift-monitor/aws"
  version = "~> 1.1"

  schedule_expression          = "cron(0 2 * * ? *)"    # find and re-run stale accounts
  report_schedule_expression   = "cron(0 8 * * ? *)"    # report on what they did
  full_run_schedule_expression = "cron(0 6 ? * MON *)"  # Monday: re-apply everything

  tags = { Project = "aft" }
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = module.aft_pipeline_drift_monitor.sns_topic_arn
  protocol  = "email"
  endpoint  = "cloud-ops@example.com"
}
```

Start with `dry_run = true` to see what would be re-run before letting it act.

To publish to a topic you already own, set `sns_topic_arn`. It takes precedence
over `create_sns_topic`, so nothing is created even at the default
`create_sns_topic = true`. Its resource policy must allow `events.amazonaws.com`
to `sns:Publish`, and if it is encrypted, pass the same key as `kms_key_arn` so
the Lambdas can publish to it — the module can only manage the policy of a topic
it creates itself. Setting `create_sns_topic = false` without an `sns_topic_arn`
fails the plan: the module has to have somewhere to publish.

Do **not** add an `aws:SourceAccount` / `aws:SourceArn` condition to either the
topic policy or the key policy for the EventBridge principal. The SNS guide states
plainly that those keys are *not supported* for EventBridge-to-encrypted-topic
publishing; a condition that is never populated denies the call and drops every
failure alert with no visible error. That is why this module's own key and topic
policies carry no condition on `events.amazonaws.com`.

### Slack, via Amazon Q Developer in chat applications

Set `enable_chatbot = true` with a workspace and channel id to have all four
signals delivered into a Slack channel instead of (or alongside) an email
subscription:

```hcl
  enable_chatbot     = true
  slack_workspace_id = "T07EA123LEP" # the workspace, not its name
  slack_channel_id   = "C07EZ1ABC23" # from the channel details, not its name
```

One prerequisite Terraform cannot do for you: **authorize the Slack workspace
once, by hand**, in the Amazon Q Developer in chat applications console, and
invite the `@Amazon Q` app to the channel. Authorizing is what produces the
workspace id above. Terraform can create the channel configuration, but not the
OAuth grant behind it.

The module creates a read-only role for Chatbot to assume, and applies
`ReadOnlyAccess` as the channel guardrail — AWS applies **`AdministratorAccess`**
when guardrails are unset, which is not a default a notification channel should
carry. Override either with `chatbot_iam_role_arn` or
`chatbot_guardrail_policy_arns`.

The encrypted topic is not a problem here, and is in fact required: Chatbot needs
a **customer-managed** key, because the AWS-managed `alias/aws/sns` key's policy
cannot be edited to let publishers use it. The module already creates a CMK for
exactly that reason. Chatbot itself needs no KMS permission — SNS decrypts before
delivery.

If you point `sns_topic_arn` at a topic this module does **not** manage, add
`sns:Subscribe` for `chatbot.amazonaws.com` to that topic's policy yourself; the
module can only write the policy of a topic it creates.

### A topic in another account

Supported. The failure-notification target publishes through an IAM role rather
than as the `events.amazonaws.com` service principal, because the roleless path
only works for a topic in the same account as the rule — EventBridge rejects a
cross-account SNS target outright with `RoleArn is required for target`.

Grant `sns:Publish` on the foreign topic to this account, which covers every
publisher through their identity policies:

```json
{
  "Sid": "AllowAftDriftMonitor",
  "Effect": "Allow",
  "Principal": { "AWS": "arn:aws:iam::<aft-account-id>:root" },
  "Action": "sns:Publish",
  "Resource": "arn:aws:sns:<region>:<topic-account-id>:<topic-name>"
}
```

To name principals individually instead, read them off the
`notification_publisher_role_arns` output — a map of every role this module
publishes with, keyed by what it is:

```
drift_detector          = the drift check's Lambda execution role
status_report           = the status report's Lambda execution role
full_run                = the weekly full run's Lambda execution role
pipeline_failed_events  = the role EventBridge assumes for failure alerts
```

The `kms_key_arn` output names the key those publishers decrypt with, so the
topic owner does not have to guess that either.

One limitation: a cross-account topic that is **encrypted with a CMK** is not
supported. Every publisher would need `kms:GenerateDataKey*` and `kms:Decrypt` on
that foreign key, and `kms_key_arn` cannot be repurposed for it because the same
key also encrypts the probe pipeline's S3 artifact bucket — pointing the artifact
store at a key in another account is not a trade this module makes for you. Use an
unencrypted cross-account topic, or a topic in this account that forwards to it.

Trigger a check on demand — `terraform output` only sees outputs the calling
root module re-exports, so see `examples/basic/outputs.tf` for the three worth
forwarding:

```bash
aws codepipeline start-pipeline-execution \
  --name "$(terraform output -raw revision_probe_pipeline_name)"

# ...or the weekly full run, without waiting for Monday
aws lambda invoke \
  --function-name "$(terraform output -raw full_run_function_name)" /dev/stdout
```

## Audit and encryption

Two controls are off by default because turning them on requires something the
module must not invent on your behalf. Both matter if your AFT deployment has
CloudTrail S3 data events disabled or mandates CMK encryption for logs.

**Object-level audit for the artifact bucket.** The probe pipeline's bucket holds
zipped copies of the customizations repositories. With AFT's S3 data events off,
reads of those archives are recorded nowhere. Set `artifact_access_log_bucket`
(and optionally `artifact_access_log_prefix`) to deliver S3 server access logs to
a bucket you already own — typically the central log-archive destination. It is
not the default because logging a bucket needs a second *permanent* bucket, and
creating one for a bucket that only holds ephemeral zips is not a decision this
module should make. The alternative is a scoped CloudTrail S3 data-event selector
on this one bucket.

**CMK-encrypted Lambda log groups.** The three log groups get retention from
`log_retention_in_days` and, by default, CloudWatch Logs' own AWS-managed
encryption. Pass `cloudwatch_logs_kms_key_id` to use a customer-managed key
instead — the same one the AFT deployment already uses for CloudWatch Logs.

Note this is **not** `kms_key_arn`. That key encrypts the SNS topic and the
artifact bucket; a CloudWatch Logs key needs a different policy (`logs.<region>.amazonaws.com`
granted `kms:Encrypt*`/`Decrypt*`/`ReEncrypt*`/`GenerateDataKey*`/`Describe*`,
scoped with a `kms:EncryptionContext:aws:logs:arn` condition), so the two are
deliberately separate inputs rather than one reused key.

## Requirements and assumptions

- **AFT >= 1.21.0.** The module invokes AFT's `aft-invoke-customizations` state
  machine with `bypass_steps = ["provisioning_bootstrap"]`, which AFT introduced
  in 1.21.0 — see
  [Why AFT >= 1.21.0, with no fallback](#why-aft--1210-with-no-fallback).
  Terraform reads `/aft/config/aft/version` and fails the plan on an older AFT.
  It also reads `/aft/config/vcs/codeconnections-connection-arn`, which AFT has
  published since
  [1.13.4](https://github.com/aws-ia/terraform-aws-control_tower_account_factory/releases/tag/1.13.4).
- AFT uses a CodeConnections-backed provider (GitHub, GitHub Enterprise Server,
  GitLab or Bitbucket). CodeCommit-based AFT installations resolve revisions
  differently and are **not** supported.
- AFT's SSM parameters exist in the account and region you deploy into:
  `/aft/config/vcs/codeconnections-connection-arn`,
  `/aft/config/{global,account}-customizations/repo-{name,branch}`,
  `/aft/config/aft/version`.
- AFT's `aft-invoke-customizations` state machine exists in the same account and
  region. Its name is fixed by AFT, so the module derives the ARN rather than
  taking it as an input.
- Pipeline names follow AFT's convention, and carry the **account id** as their
  prefix — the state machine selects accounts, so a pipeline whose name does not
  is reported as un-rerunnable rather than skipped silently. Override
  `pipeline_name_pattern` and
  `failure_pipeline_name_suffix` together if yours differ: the two select the
  same pipelines from different angles (a regex the Lambdas match, and a suffix
  that scopes their IAM permissions and the failure rule), so a precondition
  rejects a plan where they disagree rather than letting it surface at runtime as
  `AccessDenied` or as alerts that never arrive.
- Instantiate the module with a provider aimed at the **AFT management account**,
  not the Control Tower management account. In a root module whose default
  provider targets Control Tower management, pass the AFT-account provider
  explicitly - otherwise the module reads the wrong SSM parameters and tries to
  create its resources in the wrong account.

## Releases

Released from the Terraform Registry as `fivexl/aft-pipeline-drift-monitor/aws`.
The first published version is **1.1.0**; `1.0.0` was developed but never tagged.
Each release is tagged in this repository and its notes live in
[`CHANGELOG.md`](CHANGELOG.md); pin with a `~>` constraint so a breaking major
never arrives unasked.

## Costs

One probe pipeline execution per scheduled check — plus one per push while
`detect_changes` is on — each billing three action runs (two sources and the
Lambda invoke). One drift-detector invocation per probe run, one status report a
day, one full run a week. One `aft-invoke-customizations` execution per check
that found drift and one per weekly run: Step Functions Standard bills per state
transition, and its `Wait 30s` backpressure loop transitions while it waits, so a
run held behind AFT's concurrency limit for hours costs a few thousand
transitions — cents, but not zero. A customer-managed KMS key, which is **not
optional**: EventBridge cannot publish to a topic encrypted with
`alias/aws/sns`. And a few zipped copies of the customizations repositories in
S3, expiring after `artifact_retention_days` (plus one day for the non-current
version).
The re-runs themselves are AFT's normal CodeBuild cost — which you would have
paid anyway had the pipelines been kept current. The weekly full run is the one
deliberate extra: it re-applies every account once a week even when nothing
changed, so budget one full customizations pipeline execution per account per
week (several CodeBuild actions each). Note that it now reaches the whole estate
in one run rather than rotating over several weeks, so that cost arrives weekly
instead of being spread out.

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.6.1 |
| <a name="requirement_aws"></a> [aws](#requirement\_aws) | >= 6.28 |

## Modules

| Name | Source | Version |
|------|--------|---------|
| <a name="module_drift_detector"></a> [drift\_detector](#module\_drift\_detector) | terraform-aws-modules/lambda/aws | 8.2.1 |
| <a name="module_full_run"></a> [full\_run](#module\_full\_run) | terraform-aws-modules/lambda/aws | 8.2.1 |
| <a name="module_status_report"></a> [status\_report](#module\_status\_report) | terraform-aws-modules/lambda/aws | 8.2.1 |

## Resources

| Name | Type |
|------|------|
| [aws_chatbot_slack_channel_configuration.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/chatbot_slack_channel_configuration) | resource |
| [aws_cloudwatch_event_rule.daily_drift_check](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_rule) | resource |
| [aws_cloudwatch_event_rule.pipeline_failed](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_rule) | resource |
| [aws_cloudwatch_event_rule.status_report](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_rule) | resource |
| [aws_cloudwatch_event_rule.weekly_full_run](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_rule) | resource |
| [aws_cloudwatch_event_target.daily_drift_check](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_target) | resource |
| [aws_cloudwatch_event_target.pipeline_failed](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_target) | resource |
| [aws_cloudwatch_event_target.status_report](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_target) | resource |
| [aws_cloudwatch_event_target.weekly_full_run](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_event_target) | resource |
| [aws_codepipeline.revision_probe](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/codepipeline) | resource |
| [aws_iam_role.chatbot](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role.eventbridge_pipeline](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role.eventbridge_sns](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role.probe_pipeline](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role) | resource |
| [aws_iam_role_policy.chatbot](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_iam_role_policy.eventbridge_pipeline](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_iam_role_policy.eventbridge_sns](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_iam_role_policy.probe_pipeline](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy) | resource |
| [aws_kms_alias.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/kms_alias) | resource |
| [aws_kms_key.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/kms_key) | resource |
| [aws_s3_bucket.artifacts](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket) | resource |
| [aws_s3_bucket_lifecycle_configuration.artifacts](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_lifecycle_configuration) | resource |
| [aws_s3_bucket_logging.artifacts](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_logging) | resource |
| [aws_s3_bucket_policy.artifacts](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_policy) | resource |
| [aws_s3_bucket_public_access_block.artifacts](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_public_access_block) | resource |
| [aws_s3_bucket_server_side_encryption_configuration.artifacts](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_server_side_encryption_configuration) | resource |
| [aws_s3_bucket_versioning.artifacts](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_versioning) | resource |
| [aws_sns_topic.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sns_topic) | resource |
| [aws_sns_topic_policy.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sns_topic_policy) | resource |
| [terraform_data.preflight](https://registry.terraform.io/providers/hashicorp/terraform/latest/docs/resources/data) | resource |
| [aws_caller_identity.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/caller_identity) | data source |
| [aws_iam_policy_document.artifacts](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.chatbot](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.chatbot_assume](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.drift_detector](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.eventbridge_pipeline](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.eventbridge_pipeline_assume](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.eventbridge_sns](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.full_run](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.kms](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.probe_pipeline](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.probe_pipeline_assume](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.sns_topic](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.status_report](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_partition.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/partition) | data source |
| [aws_region.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/region) | data source |
| [aws_ssm_parameter.account_customizations_repo_branch](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/ssm_parameter) | data source |
| [aws_ssm_parameter.account_customizations_repo_name](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/ssm_parameter) | data source |
| [aws_ssm_parameter.aft_version](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/ssm_parameter) | data source |
| [aws_ssm_parameter.codeconnections_connection_arn](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/ssm_parameter) | data source |
| [aws_ssm_parameter.global_customizations_repo_branch](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/ssm_parameter) | data source |
| [aws_ssm_parameter.global_customizations_repo_name](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/ssm_parameter) | data source |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_artifact_access_log_bucket"></a> [artifact\_access\_log\_bucket](#input\_artifact\_access\_log\_bucket) | Name of an existing bucket to deliver S3 server access logs for the probe pipeline's artifact bucket to - typically the central log-archive destination. Leave empty for no access logging, which is the default because logging a bucket needs a second permanent bucket that this module should not create for you. Set it when the AFT deployment disables S3 data events in CloudTrail and you still want an object-level audit trail for reads of the customization source archives. The destination bucket must be in the same region and grant s3:PutObject to logging.s3.amazonaws.com for this source bucket. | `string` | `""` | no |
| <a name="input_artifact_access_log_prefix"></a> [artifact\_access\_log\_prefix](#input\_artifact\_access\_log\_prefix) | Key prefix for the delivered access logs. Ignored when artifact\_access\_log\_bucket is empty. Defaults to the artifact bucket's own name plus a slash, so one destination bucket can serve several sources without their logs interleaving. | `string` | `""` | no |
| <a name="input_artifact_bucket_name"></a> [artifact\_bucket\_name](#input\_artifact\_bucket\_name) | Name of the S3 bucket for the revision probe pipeline's artifacts. Leave empty to derive it from name\_prefix and the account id. | `string` | `""` | no |
| <a name="input_artifact_retention_days"></a> [artifact\_retention\_days](#input\_artifact\_retention\_days) | Days before probe pipeline artifacts expire. They are only used to resolve commit ids, so they have no value after the run. Non-current versions expire one day later, so total retention is this plus one. | `number` | `7` | no |
| <a name="input_chatbot_guardrail_policy_arns"></a> [chatbot\_guardrail\_policy\_arns](#input\_chatbot\_guardrail\_policy\_arns) | IAM policy ARNs applied as channel guardrails, capping what anyone can do through the channel. AWS applies AdministratorAccess when this is empty, so the default here is read-only instead. | `list(string)` | <pre>[<br/>  "arn:aws:iam::aws:policy/ReadOnlyAccess"<br/>]</pre> | no |
| <a name="input_chatbot_iam_role_arn"></a> [chatbot\_iam\_role\_arn](#input\_chatbot\_iam\_role\_arn) | ARN of an existing role for Chatbot to assume. Leave empty to have the module create a read-only one. | `string` | `""` | no |
| <a name="input_chatbot_logging_level"></a> [chatbot\_logging\_level](#input\_chatbot\_logging\_level) | CloudWatch logging level for the Chatbot configuration: ERROR, INFO or NONE. ERROR is the default because NONE hides a message Chatbot rejects (e.g. wrong format) with nothing logged anywhere. | `string` | `"ERROR"` | no |
| <a name="input_cloudwatch_logs_kms_key_id"></a> [cloudwatch\_logs\_kms\_key\_id](#input\_cloudwatch\_logs\_kms\_key\_id) | ARN of a KMS key to encrypt the three Lambda functions' CloudWatch log groups with. Leave empty to use CloudWatch Logs' own AWS-managed encryption. Pass the same customer-managed key the AFT deployment uses for CloudWatch Logs if your controls require CMK encryption there. The key policy must allow logs.<region>.amazonaws.com to kms:Encrypt*, kms:Decrypt*, kms:ReEncrypt*, kms:GenerateDataKey* and kms:Describe*, scoped with a kms:EncryptionContext:aws:logs:arn condition - note this is NOT kms\_key\_arn, which encrypts the SNS topic and the artifact bucket and is not reusable here, because a CloudWatch Logs key needs a different policy. | `string` | `""` | no |
| <a name="input_create_sns_topic"></a> [create\_sns\_topic](#input\_create\_sns\_topic) | Whether to create the notification topic. Ignored when sns\_topic\_arn is set - an existing topic always wins, so nothing is created. Set this to false only together with sns\_topic\_arn; a plan with neither fails a precondition, because the module has to have somewhere to publish. | `bool` | `true` | no |
| <a name="input_detect_changes"></a> [detect\_changes](#input\_detect\_changes) | Whether the revision probe pipeline also triggers on pushes to the customizations repositories, in addition to the daily schedule. Requires the CodeConnections connection to be able to create a webhook. | `bool` | `true` | no |
| <a name="input_drift_detector_timeout"></a> [drift\_detector\_timeout](#input\_drift\_detector\_timeout) | Timeout in seconds for the drift detector. It reads the last 10 executions of every AFT pipeline, so scale it with the number of vended accounts. | `number` | `600` | no |
| <a name="input_dry_run"></a> [dry\_run](#input\_dry\_run) | Detect and report without invoking AFT's customizations state machine. Applies to both the daily drift check and the weekly full run. Useful for the first few days in a new organisation. | `bool` | `false` | no |
| <a name="input_enable_chatbot"></a> [enable\_chatbot](#input\_enable\_chatbot) | Subscribe a Slack channel to the notification topic through Amazon Q Developer in chat applications (AWS Chatbot). Requires the Slack workspace to have been authorized once by hand in the console, which is what produces slack\_workspace\_id. slack\_workspace\_id and slack\_channel\_id are both required when this is true, enforced by a precondition. | `bool` | `false` | no |
| <a name="input_failure_pipeline_name_suffix"></a> [failure\_pipeline\_name\_suffix](#input\_failure\_pipeline\_name\_suffix) | Pipeline name suffix matched by the EventBridge failure rule, and used to scope the Lambdas' CodePipeline IAM permissions. Must be consistent with pipeline\_name\_pattern - a wrong value causes AccessDenied, not just missing alerts. An empty value is rejected: it would widen the IAM resource ARN and the EventBridge match to every CodePipeline in the account. | `string` | `"-customizations-pipeline"` | no |
| <a name="input_full_run_schedule_expression"></a> [full\_run\_schedule\_expression](#input\_full\_run\_schedule\_expression) | Schedule for the weekly full run, which starts every AFT customizations pipeline regardless of drift. Defaults to Monday 06:00 UTC - after the daily drift check, and deliberately before report\_schedule\_expression, so Monday's report describes a full run that is still in flight. | `string` | `"cron(0 6 ? * MON *)"` | no |
| <a name="input_full_run_timeout"></a> [full\_run\_timeout](#input\_full\_run\_timeout) | Timeout in seconds for the weekly full run Lambda. It makes one StartExecution call and inspects nothing, so it needs very little. | `number` | `60` | no |
| <a name="input_kms_key_arn"></a> [kms\_key\_arn](#input\_kms\_key\_arn) | ARN of an existing KMS key used for the SNS topic and the probe pipeline's artifacts. Leave empty to have the module create one. A supplied key must allow events.amazonaws.com to kms:Decrypt and kms:GenerateDataKey*, otherwise EventBridge cannot publish the failure notifications. | `string` | `""` | no |
| <a name="input_kms_key_deletion_window_in_days"></a> [kms\_key\_deletion\_window\_in\_days](#input\_kms\_key\_deletion\_window\_in\_days) | Deletion window for the KMS key created by this module. | `number` | `30` | no |
| <a name="input_lambda_ignore_source_code_hash"></a> [lambda\_ignore\_source\_code\_hash](#input\_lambda\_ignore\_source\_code\_hash) | Passed straight through to terraform-aws-modules/lambda/aws for all three Lambda functions. Suppresses a spurious plan/apply mismatch on the first apply in a fresh working directory, where the deployment archive does not exist yet when the module computes source\_code\_hash. Real code changes are still deployed: this module's Lambda functions derive their aws\_lambda\_function.filename from the content of src/ on every plan, and that filename changing is what the AWS provider actually keys a redeploy on, independent of source\_code\_hash. Defaults to true, since there is no known downside to that in this module's configuration. | `bool` | `true` | no |
| <a name="input_lambda_memory_size"></a> [lambda\_memory\_size](#input\_lambda\_memory\_size) | Memory in MB for all three Lambda functions. | `number` | `512` | no |
| <a name="input_log_level"></a> [log\_level](#input\_log\_level) | Python log level for all three Lambda functions. | `string` | `"INFO"` | no |
| <a name="input_log_retention_in_days"></a> [log\_retention\_in\_days](#input\_log\_retention\_in\_days) | CloudWatch Logs retention for all three Lambda functions. | `number` | `30` | no |
| <a name="input_name_prefix"></a> [name\_prefix](#input\_name\_prefix) | Prefix for every resource name created by this module. | `string` | `"aft-pipeline-drift-monitor"` | no |
| <a name="input_notify_full_run_when_clean"></a> [notify\_full\_run\_when\_clean](#input\_notify\_full\_run\_when\_clean) | Publish the weekly full run summary even when the invocation was a duplicate of an earlier delivery of the same scheduled event, so nothing was started twice. Defaults to false, so only an actionable summary (a fresh invocation, a failed invocation, or a dry run) is sent. Does not affect the drift check or the scheduled status report, which have their own notification behaviour. | `bool` | `false` | no |
| <a name="input_notify_on_drift"></a> [notify\_on\_drift](#input\_notify\_on\_drift) | Publish an SNS summary for each drift check that found something to report - drifted, skipped, failing-on-HEAD, quarantined or uninspectable pipelines. Operational failures (a state machine invocation that did not land, a quarantined or uninspectable pipeline) are published regardless of this flag: it gates the informational summary, not failures. Does not affect the scheduled status report or the weekly full run summary, which have their own notification behaviour, nor the EventBridge failure alerts, which are always published. | `bool` | `true` | no |
| <a name="input_notify_status_report_when_clean"></a> [notify\_status\_report\_when\_clean](#input\_notify\_status\_report\_when\_clean) | Publish the scheduled status report even when every pipeline is current - no failures and nothing behind HEAD. Defaults to false, so only an actionable report (something failed, or HEAD could not be resolved/was only partially resolved) is sent. Does not affect the drift check or the weekly full run summary, which have their own notification behaviour. | `bool` | `false` | no |
| <a name="input_pipeline_name_pattern"></a> [pipeline\_name\_pattern](#input\_pipeline\_name\_pattern) | Python regular expression the Lambdas use to select AFT customizations pipelines. The default matches AFT's own naming, `<account-id>-customizations-pipeline`. | `string` | `"^\\d{12}-customizations-pipeline$"` | no |
| <a name="input_python_runtime"></a> [python\_runtime](#input\_python\_runtime) | Lambda Python runtime. | `string` | `"python3.14"` | no |
| <a name="input_report_schedule_expression"></a> [report\_schedule\_expression](#input\_report\_schedule\_expression) | Schedule for the status report. Set it a few hours after schedule\_expression so the pipelines started by the drift check have finished. | `string` | `"cron(0 8 * * ? *)"` | no |
| <a name="input_schedule_expression"></a> [schedule\_expression](#input\_schedule\_expression) | Schedule for the daily drift check. Starts the revision probe pipeline, which resolves HEAD through the AFT CodeConnections connection and then invokes the drift detector. | `string` | `"cron(0 2 * * ? *)"` | no |
| <a name="input_slack_channel_id"></a> [slack\_channel\_id](#input\_slack\_channel\_id) | Slack channel id the notifications are posted to. Looks like C07EZ1ABC23 - copy it from the channel details, not the channel name. | `string` | `""` | no |
| <a name="input_slack_workspace_id"></a> [slack\_workspace\_id](#input\_slack\_workspace\_id) | Slack workspace (team) id, as returned when you authorize the workspace in the Amazon Q Developer in chat applications console. Looks like T07EA123LEP. | `string` | `""` | no |
| <a name="input_sns_topic_arn"></a> [sns\_topic\_arn](#input\_sns\_topic\_arn) | ARN of an existing SNS topic to publish to, in this account or another. Takes precedence over create\_sns\_topic. The topic policy must allow sns:Publish to this account or to the specific principals: the three Lambda roles and the pipeline\_failed\_target\_role\_arn output. An encrypted topic in another account is not supported - see the README. | `string` | `""` | no |
| <a name="input_status_report_timeout"></a> [status\_report\_timeout](#input\_status\_report\_timeout) | Timeout in seconds for the status report Lambda. | `number` | `300` | no |
| <a name="input_tags"></a> [tags](#input\_tags) | Tags applied to every resource that supports them. | `map(string)` | `{}` | no |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_artifact_bucket_name"></a> [artifact\_bucket\_name](#output\_artifact\_bucket\_name) | Name of the S3 bucket holding the revision probe pipeline's artifacts. |
| <a name="output_chatbot_configuration_arn"></a> [chatbot\_configuration\_arn](#output\_chatbot\_configuration\_arn) | ARN of the Chatbot Slack channel configuration, or null when enable\_chatbot is false. |
| <a name="output_chatbot_iam_role_arn"></a> [chatbot\_iam\_role\_arn](#output\_chatbot\_iam\_role\_arn) | Role Chatbot assumes - the one supplied via chatbot\_iam\_role\_arn, the one this module created, or null when enable\_chatbot is false. |
| <a name="output_daily_drift_check_rule_name"></a> [daily\_drift\_check\_rule\_name](#output\_daily\_drift\_check\_rule\_name) | Name of the EventBridge rule that runs the daily drift check. |
| <a name="output_drift_detector_function_arn"></a> [drift\_detector\_function\_arn](#output\_drift\_detector\_function\_arn) | ARN of the drift detector Lambda function. |
| <a name="output_drift_detector_function_name"></a> [drift\_detector\_function\_name](#output\_drift\_detector\_function\_name) | Name of the drift detector Lambda function. |
| <a name="output_full_run_function_arn"></a> [full\_run\_function\_arn](#output\_full\_run\_function\_arn) | ARN of the weekly full run Lambda function. |
| <a name="output_full_run_function_name"></a> [full\_run\_function\_name](#output\_full\_run\_function\_name) | Name of the weekly full run Lambda function. |
| <a name="output_kms_key_arn"></a> [kms\_key\_arn](#output\_kms\_key\_arn) | ARN of the key encrypting the notification topic and the probe pipeline's artifacts - either the one supplied via kms\_key\_arn or the one this module created. A cross-account topic owner needs this to know which key its publishers decrypt with. |
| <a name="output_notification_publisher_role_arns"></a> [notification\_publisher\_role\_arns](#output\_notification\_publisher\_role\_arns) | Every principal this module publishes to the notification topic with, keyed by what it is. When sns\_topic\_arn points at a topic this module does not manage, its owner has to authorize exactly these - so they are output by name rather than left to be guessed from the generated role names or discovered as a runtime AccessDenied. Granting sns:Publish to this account covers all of them at once. |
| <a name="output_pipeline_failed_rule_name"></a> [pipeline\_failed\_rule\_name](#output\_pipeline\_failed\_rule\_name) | Name of the EventBridge rule that forwards pipeline failures to SNS. |
| <a name="output_pipeline_failed_target_role_arn"></a> [pipeline\_failed\_target\_role\_arn](#output\_pipeline\_failed\_target\_role\_arn) | Role EventBridge assumes to publish failure notifications. A topic in another account must allow this role (or this account) to sns:Publish. |
| <a name="output_revision_probe_pipeline_arn"></a> [revision\_probe\_pipeline\_arn](#output\_revision\_probe\_pipeline\_arn) | ARN of the revision probe pipeline. |
| <a name="output_revision_probe_pipeline_name"></a> [revision\_probe\_pipeline\_name](#output\_revision\_probe\_pipeline\_name) | Name of the pipeline that resolves HEAD of the customizations repositories through the AFT CodeConnections connection. |
| <a name="output_sns_topic_arn"></a> [sns\_topic\_arn](#output\_sns\_topic\_arn) | ARN of the topic every notification is published to - either the one supplied by the caller or the one this module created. |
| <a name="output_status_report_function_arn"></a> [status\_report\_function\_arn](#output\_status\_report\_function\_arn) | ARN of the status report Lambda function. |
| <a name="output_status_report_function_name"></a> [status\_report\_function\_name](#output\_status\_report\_function\_name) | Name of the status report Lambda function. |
| <a name="output_status_report_rule_name"></a> [status\_report\_rule\_name](#output\_status\_report\_rule\_name) | Name of the EventBridge rule that runs the status report. |
| <a name="output_weekly_full_run_rule_name"></a> [weekly\_full\_run\_rule\_name](#output\_weekly\_full\_run\_rule\_name) | Name of the EventBridge rule that re-applies the customizations to every AFT-managed account weekly. |
<!-- END_TF_DOCS -->

## License

Apache 2.0 — see [LICENSE](LICENSE).
