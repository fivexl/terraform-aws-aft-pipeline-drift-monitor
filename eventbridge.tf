########################################################################
# Daily drift check
#
# The schedule starts the revision probe pipeline rather than the Lambda
# directly: the pipeline is what resolves HEAD through the CodeConnections
# connection, and it invokes the drift detector as its second stage.
########################################################################

resource "aws_cloudwatch_event_rule" "daily_drift_check" {
  name                = "${var.name_prefix}-daily-drift-check"
  description         = "Daily check for AFT customizations pipelines running stale commits"
  schedule_expression = var.schedule_expression
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "daily_drift_check" {
  rule      = aws_cloudwatch_event_rule.daily_drift_check.name
  target_id = "revision-probe-pipeline"
  arn       = aws_codepipeline.revision_probe.arn
  role_arn  = aws_iam_role.eventbridge_pipeline.arn
}

data "aws_iam_policy_document" "eventbridge_pipeline_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "eventbridge_pipeline" {
  name               = "${var.name_prefix}-eventbridge"
  description        = "Lets EventBridge start the AFT revision probe pipeline"
  assume_role_policy = data.aws_iam_policy_document.eventbridge_pipeline_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "eventbridge_pipeline" {
  statement {
    sid       = "StartRevisionProbe"
    actions   = ["codepipeline:StartPipelineExecution"]
    resources = [aws_codepipeline.revision_probe.arn]
  }
}

resource "aws_iam_role_policy" "eventbridge_pipeline" {
  name   = "${var.name_prefix}-start-revision-probe"
  role   = aws_iam_role.eventbridge_pipeline.id
  policy = data.aws_iam_policy_document.eventbridge_pipeline.json
}

########################################################################
# Pipeline failure notifications
#
# Straight to SNS with an input transformer: a Lambda would add nothing but
# a cold start. Covers every failure, including runs this module did not
# start and the probe pipeline's own failures.
########################################################################

resource "aws_cloudwatch_event_rule" "pipeline_failed" {
  name        = "${var.name_prefix}-pipeline-failed"
  description = "Notify on AFT customizations pipeline failures"
  tags        = var.tags

  event_pattern = jsonencode({
    source        = ["aws.codepipeline"]
    "detail-type" = ["CodePipeline Pipeline Execution State Change"]
    detail = {
      state = ["FAILED"]
      pipeline = [
        { suffix = var.failure_pipeline_name_suffix },
        local.probe_pipeline_name,
      ]
    }
  })
}

resource "aws_cloudwatch_event_target" "pipeline_failed" {
  rule      = aws_cloudwatch_event_rule.pipeline_failed.name
  target_id = "sns"
  arn       = local.sns_topic_arn

  input_transformer {
    input_paths = {
      pipeline  = "$.detail.pipeline"
      execution = "$.detail.execution-id"
      region    = "$.region"
      time      = "$.time"
    }

    # A bare JSON string becomes the SNS message body verbatim.
    input_template = "\"AFT pipeline <pipeline> FAILED at <time> (execution <execution>). https://<region>.console.aws.amazon.com/codesuite/codepipeline/pipelines/<pipeline>/executions/<execution>?region=<region>\""
  }
}

########################################################################
# Status report
########################################################################

resource "aws_cloudwatch_event_rule" "status_report" {
  name                = "${var.name_prefix}-status-report"
  description         = "Report AFT customizations pipeline outcomes and remaining drift"
  schedule_expression = var.report_schedule_expression
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "status_report" {
  rule      = aws_cloudwatch_event_rule.status_report.name
  target_id = "status-report-lambda"
  arn       = module.status_report.lambda_function_arn
}
