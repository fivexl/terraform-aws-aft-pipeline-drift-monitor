provider "aws" {
  # Change to your AFT home region.
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

  # Monday 06:00 UTC: re-run EVERY pipeline, drift or not, to correct drift that
  # happened inside an account rather than in the repository. Push this out to a
  # far-future cron if you only want commit-driven re-runs.
  full_run_schedule_expression = "cron(0 6 ? * MON *)"

  # Keep this at or above your account count for a full sweep every week; below
  # it, the run rotates oldest-first across successive weeks.

  # Don't stampede CodeBuild and the account Terraform states.
  max_pipelines_per_run = 20

  # Deliver every signal into Slack. Authorize the workspace once by hand in the
  # Amazon Q Developer in chat applications console first - that is what gives
  # you the workspace id, and Terraform cannot create the OAuth grant.
  # enable_chatbot     = true
  # slack_workspace_id = "T07EA123LEP"
  # slack_channel_id   = "C07EZ1ABC23"

  tags = {
    Project = "aft"
  }
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = module.aft_pipeline_drift_monitor.sns_topic_arn
  protocol  = "email"
  endpoint  = "cloud-ops@example.com"
}
