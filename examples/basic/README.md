# Basic example

Deploys the AFT customizations pipeline drift monitor with its defaults: a daily
drift check at 02:00 UTC, a status report at 08:00 UTC, and a module-created SNS
topic with one email subscription.

## Prerequisites

- Credentials for the **AFT management account**, in the **AFT home region**.
- AFT deployed with a Git provider that uses CodeConnections (GitHub, GitLab or
  Bitbucket). The module reads the connection ARN and the customizations
  repository names from AFT's SSM parameters, so nothing has to be passed in.

## Usage

```bash
terraform init
terraform plan
terraform apply
```

Confirm the email subscription, then trigger a check without waiting for the
schedule:

```bash
aws codepipeline start-pipeline-execution \
  --name "$(terraform output -raw revision_probe_pipeline_name)"
```

To see what it *would* do before letting it start any pipeline, set
`dry_run = true` on the first apply.

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
| ---- | ------- |
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.5.7 |
| <a name="requirement_aws"></a> [aws](#requirement\_aws) | >= 6.28 |

## Providers

| Name | Version |
| ---- | ------- |
| <a name="provider_aws"></a> [aws](#provider\_aws) | >= 6.28 |

## Modules

| Name | Source | Version |
| ---- | ------ | ------- |
| <a name="module_aft_pipeline_drift_monitor"></a> [aft\_pipeline\_drift\_monitor](#module\_aft\_pipeline\_drift\_monitor) | fivexl/aft-pipeline-drift-monitor/aws | ~> 1.0 |

## Resources

| Name | Type |
| ---- | ---- |
| [aws_sns_topic_subscription.email](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sns_topic_subscription) | resource |

## Inputs

No inputs.

## Outputs

No outputs.
<!-- END_TF_DOCS -->