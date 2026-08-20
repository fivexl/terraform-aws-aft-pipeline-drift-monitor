# Basic example

Deploys the AFT customizations pipeline drift monitor with its defaults: a daily
drift check at 02:00 UTC, a status report at 08:00 UTC, a weekly full run every
Monday at 06:00 UTC, and a module-created SNS topic with one email subscription.

Note what the weekly full run does before you apply this: it starts **every** AFT
customizations pipeline whether or not its commit is behind HEAD, so every account
is re-applied once a week. That is what corrects drift made *inside* an account
(console changes), which no commit comparison can see. If you only want
commit-driven re-runs, push `full_run_schedule_expression` out to a far-future
cron.

## Prerequisites

- Credentials for the **AFT management account**, in the **AFT home region**.
- AFT deployed with a Git provider that uses CodeConnections (GitHub, GitHub
  Enterprise Server, GitLab or Bitbucket). The module reads the connection ARN and the customizations
  repository names from AFT's SSM parameters, so nothing has to be passed in.

## Usage

```bash
terraform init
terraform plan
terraform apply
```

Confirm the email subscription, then trigger a drift check without waiting for the
schedule:

```bash
aws codepipeline start-pipeline-execution \
  --name "$(terraform output -raw revision_probe_pipeline_name)"
```

Or trigger the full run:

```bash
aws lambda invoke \
  --function-name "$(terraform output -raw full_run_function_name)" \
  /dev/stdout
```

To see what either would do before letting it start any pipeline, set
`dry_run = true` on the first apply.

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.9.0 |
| <a name="requirement_aws"></a> [aws](#requirement\_aws) | >= 6.28 |

## Modules

| Name | Source | Version |
|------|--------|---------|
| <a name="module_aft_pipeline_drift_monitor"></a> [aft\_pipeline\_drift\_monitor](#module\_aft\_pipeline\_drift\_monitor) | fivexl/aft-pipeline-drift-monitor/aws | ~> 1.0 |

## Resources

| Name | Type |
|------|------|
| [aws_sns_topic_subscription.email](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sns_topic_subscription) | resource |

## Inputs

No inputs.

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_full_run_function_name"></a> [full\_run\_function\_name](#output\_full\_run\_function\_name) | Invoke this function to run every pipeline on demand, drift or not. |
| <a name="output_revision_probe_pipeline_name"></a> [revision\_probe\_pipeline\_name](#output\_revision\_probe\_pipeline\_name) | Start this pipeline to run a drift check on demand. |
| <a name="output_sns_topic_arn"></a> [sns\_topic\_arn](#output\_sns\_topic\_arn) | Topic every drift summary, failure alert and status report is published to. |
<!-- END_TF_DOCS -->
