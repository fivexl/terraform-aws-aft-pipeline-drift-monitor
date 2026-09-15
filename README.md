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
                          ├──► start_pipeline_execution on the stale ones
                          └──► SNS: "N pipelines behind HEAD, started N"
                                 │
EventBridge (any *-customizations-pipeline FAILED) ───────────┤
                                                                │
EventBridge (a few hours later) ──► status report Lambda ─────┤
                                                                │
EventBridge (weekly) ──────────────► full run Lambda ──────────┤
                                       starts EVERY pipeline    │
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
| Drift summary | `drift-detector` Lambda | Each drift check with something to report: stale, failing-on-HEAD or unstartable pipelines |
| Failure alert | EventBridge → SNS directly, via an IAM role | Any pipeline ending in `failure_pipeline_name_suffix` fails, plus the revision probe itself |
| Status report | `status-report` Lambda | On its own schedule, a few hours after the check |
| Weekly full run | `full-run` Lambda | Weekly, after starting every pipeline |

All four go to one SNS topic, so one subscription covers the whole module —
email, or Slack via `enable_chatbot` (see below). Both can be attached to the
same topic at once; they are independent delivery paths, not alternatives you
choose between once and for all.

## Two different kinds of drift

The daily check and the weekly full run answer different questions, which is why
both exist:

- **Repository drift** — the account is applied at an older commit than HEAD. The
  drift detector finds this and re-runs only the affected accounts.
- **Account drift** — the account no longer matches the customizations that were
  applied to it, because somebody changed something in the console. No commit
  comparison can see this. The weekly full run corrects it by re-applying the
  customizations to **every** account whether or not its commit is current.

The full run skips pipelines with an execution already in flight: those pipelines
run in `SUPERSEDED` mode, so a second execution would queue behind the running one
and then supersede it, achieving nothing the in-flight run is not already doing.

It obeys `dry_run` the same way the drift check does, and selects **oldest last
execution first** — so a cap below your account count rotates through every
account over successive weeks instead of starting the same lexicographically-first
accounts forever.

The full run has its own cap, `full_run_max_pipelines_per_run`, independent of the
drift check's `max_pipelines_per_run`. They default to the same value, but the two
answer different questions: the drift check only has to start pipelines that
actually drifted, while the full run starts every account regardless, so the same
cap that comfortably covers daily drift can leave the full run needing several
weeks to reach every account (at the default of 20, a 50-account organisation
takes about three weeks per full sweep). Set `full_run_max_pipelines_per_run` at
or above your account count if you want every account re-applied every week.

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
- the newest execution already ran HEAD and did not succeed — another run would
  only repeat the same failure, so it is listed as *failing on HEAD* instead

At most `max_pipelines_per_run` pipelines are started per check; the rest are
deferred to the next run, which keeps CodeBuild concurrency and per-account
Terraform state contention bounded. Drift is judged by source **action name**, so
if AFT ever renames or adds one the detector fails loudly rather than marking
every account drifted — set `SOURCE_ACTIONS` is compared against what the probe
resolved.

## Usage

Deploy into the **AFT management account**, in the **AFT home region**.

```hcl
module "aft_pipeline_drift_monitor" {
  source  = "fivexl/aft-pipeline-drift-monitor/aws"
  version = "~> 1.0"

  schedule_expression          = "cron(0 2 * * ? *)"    # find and re-run stale pipelines
  report_schedule_expression   = "cron(0 8 * * ? *)"    # report on what they did
  full_run_schedule_expression = "cron(0 6 ? * MON *)"  # Monday: re-run everything
  max_pipelines_per_run        = 20

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

To name principals individually instead, they are the three Lambda execution
roles and the `pipeline_failed_target_role_arn` output.

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

## Requirements and assumptions

- AFT uses a CodeConnections-backed provider (GitHub, GitHub Enterprise Server,
  GitLab or Bitbucket). CodeCommit-based AFT installations resolve revisions
  differently and are **not** supported.
- AFT's SSM parameters exist in the account and region you deploy into:
  `/aft/config/vcs/codeconnections-connection-arn`,
  `/aft/config/{global,account}-customizations/repo-{name,branch}`.
- Pipeline names follow AFT's convention. Override `pipeline_name_pattern` and
  `failure_pipeline_name_suffix` together if yours differ.

## Costs

One probe pipeline execution per scheduled check — plus one per push while
`detect_changes` is on — each billing three action runs (two sources and the
Lambda invoke). One drift-detector invocation per probe run, one status report a
day, one full run a week. A customer-managed KMS key, which is **not optional**:
EventBridge cannot publish to a topic encrypted with `alias/aws/sns`. And a few
zipped copies of the customizations repositories in S3, expiring after
`artifact_retention_days` (plus one day for the non-current version).
The re-runs themselves are AFT's normal CodeBuild cost — which you would have
paid anyway had the pipelines been kept current. The weekly full run is the one
deliberate extra: it re-applies every account once a week even when nothing
changed, so budget one full customizations pipeline execution per account per
week (several CodeBuild actions each).

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.9.0 |
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
| [aws_s3_bucket_policy.artifacts](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_policy) | resource |
| [aws_s3_bucket_public_access_block.artifacts](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_public_access_block) | resource |
| [aws_s3_bucket_server_side_encryption_configuration.artifacts](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_server_side_encryption_configuration) | resource |
| [aws_s3_bucket_versioning.artifacts](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/s3_bucket_versioning) | resource |
| [aws_sns_topic.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sns_topic) | resource |
| [aws_sns_topic_policy.this](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sns_topic_policy) | resource |
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
| [aws_ssm_parameter.account_customizations_repo_branch](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/ssm_parameter) | data source |
| [aws_ssm_parameter.account_customizations_repo_name](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/ssm_parameter) | data source |
| [aws_ssm_parameter.codeconnections_connection_arn](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/ssm_parameter) | data source |
| [aws_ssm_parameter.global_customizations_repo_branch](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/ssm_parameter) | data source |
| [aws_ssm_parameter.global_customizations_repo_name](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/ssm_parameter) | data source |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_artifact_bucket_name"></a> [artifact\_bucket\_name](#input\_artifact\_bucket\_name) | Name of the S3 bucket for the revision probe pipeline's artifacts. Leave empty to derive it from name\_prefix and the account id. | `string` | `""` | no |
| <a name="input_artifact_retention_days"></a> [artifact\_retention\_days](#input\_artifact\_retention\_days) | Days before probe pipeline artifacts expire. They are only used to resolve commit ids, so they have no value after the run. Non-current versions expire one day later, so total retention is this plus one. | `number` | `7` | no |
| <a name="input_chatbot_guardrail_policy_arns"></a> [chatbot\_guardrail\_policy\_arns](#input\_chatbot\_guardrail\_policy\_arns) | IAM policy ARNs applied as channel guardrails, capping what anyone can do through the channel. AWS applies AdministratorAccess when this is empty, so the default here is read-only instead. | `list(string)` | <pre>[<br/>  "arn:aws:iam::aws:policy/ReadOnlyAccess"<br/>]</pre> | no |
| <a name="input_chatbot_iam_role_arn"></a> [chatbot\_iam\_role\_arn](#input\_chatbot\_iam\_role\_arn) | ARN of an existing role for Chatbot to assume. Leave empty to have the module create a read-only one. | `string` | `""` | no |
| <a name="input_chatbot_logging_level"></a> [chatbot\_logging\_level](#input\_chatbot\_logging\_level) | CloudWatch logging level for the Chatbot configuration: ERROR, INFO or NONE. ERROR is the default because NONE hides a message Chatbot rejects (e.g. wrong format) with nothing logged anywhere. | `string` | `"ERROR"` | no |
| <a name="input_create_sns_topic"></a> [create\_sns\_topic](#input\_create\_sns\_topic) | Whether to create the notification topic. Ignored when sns\_topic\_arn is set - an existing topic always wins, so nothing is created. Set this to false only together with sns\_topic\_arn. | `bool` | `true` | no |
| <a name="input_detect_changes"></a> [detect\_changes](#input\_detect\_changes) | Whether the revision probe pipeline also triggers on pushes to the customizations repositories, in addition to the daily schedule. Requires the CodeConnections connection to be able to create a webhook. | `bool` | `true` | no |
| <a name="input_drift_detector_timeout"></a> [drift\_detector\_timeout](#input\_drift\_detector\_timeout) | Timeout in seconds for the drift detector. It reads the last 10 executions of every AFT pipeline, so scale it with the number of vended accounts. | `number` | `600` | no |
| <a name="input_dry_run"></a> [dry\_run](#input\_dry\_run) | Detect and report without starting any AFT pipeline. Applies to both the daily drift check and the weekly full run. Useful for the first few days in a new organisation. | `bool` | `false` | no |
| <a name="input_enable_chatbot"></a> [enable\_chatbot](#input\_enable\_chatbot) | Subscribe a Slack channel to the notification topic through Amazon Q Developer in chat applications (AWS Chatbot). Requires the Slack workspace to have been authorized once by hand in the console, which is what produces slack\_workspace\_id. | `bool` | `false` | no |
| <a name="input_failure_pipeline_name_suffix"></a> [failure\_pipeline\_name\_suffix](#input\_failure\_pipeline\_name\_suffix) | Pipeline name suffix matched by the EventBridge failure rule, and used to scope the Lambdas' CodePipeline IAM permissions. Must be consistent with pipeline\_name\_pattern - a wrong value causes AccessDenied, not just missing alerts. | `string` | `"-customizations-pipeline"` | no |
| <a name="input_full_run_max_pipelines_per_run"></a> [full\_run\_max\_pipelines\_per\_run](#input\_full\_run\_max\_pipelines\_per\_run) | Maximum number of AFT pipelines to start in a single weekly full run. Defaults to max\_pipelines\_per\_run, but the two are independent: the daily drift check only has to start pipelines that actually drifted, while the full run starts every account regardless, so the same cap can be too low to cover the whole estate weekly. The full run selects oldest-execution-first, so a cap below your account count rotates through every account over successive weeks rather than starving the same accounts - set this at or above your account count if you want every account re-applied every week. Set to -1 to reuse max\_pipelines\_per\_run (the default). | `number` | `-1` | no |
| <a name="input_full_run_schedule_expression"></a> [full\_run\_schedule\_expression](#input\_full\_run\_schedule\_expression) | Schedule for the weekly full run, which starts every AFT customizations pipeline regardless of drift. Defaults to Monday 06:00 UTC - after the daily drift check, and deliberately before report\_schedule\_expression, so Monday's report describes a full run that is still in flight. | `string` | `"cron(0 6 ? * MON *)"` | no |
| <a name="input_full_run_timeout"></a> [full\_run\_timeout](#input\_full\_run\_timeout) | Timeout in seconds for the weekly full run Lambda. It reads the last 10 executions of every AFT pipeline before starting it, so scale it with the number of vended accounts. | `number` | `600` | no |
| <a name="input_kms_key_arn"></a> [kms\_key\_arn](#input\_kms\_key\_arn) | ARN of an existing KMS key used for the SNS topic and the probe pipeline's artifacts. Leave empty to have the module create one. A supplied key must allow events.amazonaws.com to kms:Decrypt and kms:GenerateDataKey*, otherwise EventBridge cannot publish the failure notifications. | `string` | `""` | no |
| <a name="input_kms_key_deletion_window_in_days"></a> [kms\_key\_deletion\_window\_in\_days](#input\_kms\_key\_deletion\_window\_in\_days) | Deletion window for the KMS key created by this module. | `number` | `30` | no |
| <a name="input_lambda_ignore_source_code_hash"></a> [lambda\_ignore\_source\_code\_hash](#input\_lambda\_ignore\_source\_code\_hash) | Passed straight through to terraform-aws-modules/lambda/aws for all three Lambda functions. Suppresses a spurious plan/apply mismatch on the first apply in a fresh working directory, where the deployment archive does not exist yet when the module computes source\_code\_hash. Real code changes are still deployed: this module's Lambda functions derive their aws\_lambda\_function.filename from the content of src/ on every plan, and that filename changing is what the AWS provider actually keys a redeploy on, independent of source\_code\_hash. Defaults to true, since there is no known downside to that in this module's configuration. | `bool` | `true` | no |
| <a name="input_lambda_memory_size"></a> [lambda\_memory\_size](#input\_lambda\_memory\_size) | Memory in MB for all three Lambda functions. | `number` | `512` | no |
| <a name="input_log_level"></a> [log\_level](#input\_log\_level) | Python log level for all three Lambda functions. | `string` | `"INFO"` | no |
| <a name="input_log_retention_in_days"></a> [log\_retention\_in\_days](#input\_log\_retention\_in\_days) | CloudWatch Logs retention for all three Lambda functions. | `number` | `30` | no |
| <a name="input_max_pipelines_per_run"></a> [max\_pipelines\_per\_run](#input\_max\_pipelines\_per\_run) | Maximum number of AFT pipelines to start in a single drift check. The remainder is deferred to the next run, which keeps CodeBuild concurrency and Terraform state contention under control. | `number` | `20` | no |
| <a name="input_name_prefix"></a> [name\_prefix](#input\_name\_prefix) | Prefix for every resource name created by this module. | `string` | `"aft-pipeline-drift-monitor"` | no |
| <a name="input_notify_full_run_when_clean"></a> [notify\_full\_run\_when\_clean](#input\_notify\_full\_run\_when\_clean) | Publish the weekly full run summary even when nothing was started and nothing failed to start - every pipeline was already running, or there was simply nothing eligible. Defaults to false, so only an actionable summary (something started, something failed to start, a dry run, or no matching pipeline) is sent. Does not affect the drift check or the scheduled status report, which have their own notification behaviour. | `bool` | `false` | no |
| <a name="input_notify_on_drift"></a> [notify\_on\_drift](#input\_notify\_on\_drift) | Publish an SNS summary for each drift check that found something to report - drifted, started, skipped, failing-on-HEAD or unstartable pipelines. Does not affect the scheduled status report or the weekly full run summary, which have their own notification behaviour, nor the EventBridge failure alerts, which are always published. | `bool` | `true` | no |
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
| <a name="output_pipeline_failed_rule_name"></a> [pipeline\_failed\_rule\_name](#output\_pipeline\_failed\_rule\_name) | Name of the EventBridge rule that forwards pipeline failures to SNS. |
| <a name="output_pipeline_failed_target_role_arn"></a> [pipeline\_failed\_target\_role\_arn](#output\_pipeline\_failed\_target\_role\_arn) | Role EventBridge assumes to publish failure notifications. A topic in another account must allow this role (or this account) to sns:Publish. |
| <a name="output_revision_probe_pipeline_arn"></a> [revision\_probe\_pipeline\_arn](#output\_revision\_probe\_pipeline\_arn) | ARN of the revision probe pipeline. |
| <a name="output_revision_probe_pipeline_name"></a> [revision\_probe\_pipeline\_name](#output\_revision\_probe\_pipeline\_name) | Name of the pipeline that resolves HEAD of the customizations repositories through the AFT CodeConnections connection. |
| <a name="output_sns_topic_arn"></a> [sns\_topic\_arn](#output\_sns\_topic\_arn) | ARN of the topic every notification is published to - either the one supplied by the caller or the one this module created. |
| <a name="output_status_report_function_arn"></a> [status\_report\_function\_arn](#output\_status\_report\_function\_arn) | ARN of the status report Lambda function. |
| <a name="output_status_report_function_name"></a> [status\_report\_function\_name](#output\_status\_report\_function\_name) | Name of the status report Lambda function. |
| <a name="output_status_report_rule_name"></a> [status\_report\_rule\_name](#output\_status\_report\_rule\_name) | Name of the EventBridge rule that runs the status report. |
| <a name="output_weekly_full_run_rule_name"></a> [weekly\_full\_run\_rule\_name](#output\_weekly\_full\_run\_rule\_name) | Name of the EventBridge rule that runs every pipeline weekly. |
<!-- END_TF_DOCS -->

## License

Apache 2.0 — see [LICENSE](LICENSE).
