provider "aws" {
  region = "eu-central-1"
}

# Deploy into the AFT management account, in the AFT home region: the module
# reads AFT's own SSM parameters and inspects the customizations pipelines,
# both of which live there.
module "aft_pipeline_drift_monitor" {
  source  = "fivexl/aft-pipeline-drift-monitor/aws"
  version = "~> 1.0"

  # Daily at 02:00 UTC: resolve HEAD and re-run every stale pipeline.
  schedule_expression = "cron(0 2 * * ? *)"

  # 08:00 UTC: report what those runs did.
  report_schedule_expression = "cron(0 8 * * ? *)"

  # Don't stampede CodeBuild and the account Terraform states.
  max_pipelines_per_run = 20

  tags = {
    Project = "aft"
  }
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = module.aft_pipeline_drift_monitor.sns_topic_arn
  protocol  = "email"
  endpoint  = "cloud-ops@example.com"
}
