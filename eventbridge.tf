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
#
# The target carries a role_arn. For an SNS target EventBridge accepts either
# an execution role or the topic's resource policy, but the resource-policy
# path authenticates as the events.amazonaws.com service principal and only
# works for a topic in this account - a cross-account topic is rejected at
# PutTargets with "RoleArn is required for target". Publishing as a role works
# for both, so the module always uses one.
# https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-use-resource-based.html
########################################################################

resource "aws_iam_role" "eventbridge_sns" {
  name               = "${var.name_prefix}-eventbridge-sns"
  description        = "Lets EventBridge publish AFT pipeline failure notifications to SNS"
  assume_role_policy = data.aws_iam_policy_document.eventbridge_pipeline_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "eventbridge_sns" {
  statement {
    sid       = "PublishFailureNotifications"
    actions   = ["sns:Publish"]
    resources = [local.sns_topic_arn]
  }

  statement {
    sid = "UseEncryptionKey"
    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey",
      "kms:GenerateDataKeyWithoutPlaintext",
    ]
    resources = [local.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "eventbridge_sns" {
  name   = "${var.name_prefix}-publish-failure-notifications"
  role   = aws_iam_role.eventbridge_sns.id
  policy = data.aws_iam_policy_document.eventbridge_sns.json
}

resource "aws_cloudwatch_event_rule" "pipeline_failed" {
  name        = "${var.name_prefix}-pipeline-failed"
  description = "Notify on AFT customizations pipeline failures"
  tags        = var.tags

  # pipeline_name_pattern selects which pipelines the Lambdas inspect and start;
  # failure_pipeline_name_suffix scopes this rule's match AND the Lambdas' IAM
  # pipeline ARNs. Two independent selectors for one set of pipelines means a
  # mismatch is silent until runtime, where it surfaces as AccessDenied on
  # StartPipelineExecution or as failure alerts that never arrive.
  #
  # The check builds the name AFT would give a pipeline with this suffix and
  # asserts the pattern matches it, so the two selectors are proven to agree on
  # at least one realistic name rather than merely both being non-empty. It is a
  # precondition rather than a variable validation so the module keeps working on
  # Terraform versions that do not allow a validation to reference another
  # variable.
  lifecycle {
    precondition {
      condition     = can(regex(var.pipeline_name_pattern, "123456789012${var.failure_pipeline_name_suffix}"))
      error_message = "pipeline_name_pattern (${var.pipeline_name_pattern}) does not match a pipeline named 123456789012${var.failure_pipeline_name_suffix}, so it and failure_pipeline_name_suffix select different pipelines. The Lambdas would be denied StartPipelineExecution on the pipelines they discover, and failures on them would raise no alert. Set both to describe the same names."
    }
  }

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
  role_arn  = aws_iam_role.eventbridge_sns.arn

  input_transformer {
    input_paths = {
      pipeline  = "$.detail.pipeline"
      execution = "$.detail.execution-id"
      region    = "$.region"
      time      = "$.time"
    }

    # AWS Chatbot only renders default service events or its custom
    # notification schema (see aft_pipelines.chatbot_envelope) - a bare string
    # is silently discarded. A plain subscriber (e.g. email) gets raw JSON if
    # it received the envelope instead, so the shape is conditional on
    # enable_chatbot rather than always wrapped.
    input_template = var.enable_chatbot ? jsonencode({
      version = "1.0"
      source  = "custom"
      content = {
        textType    = "client-markdown"
        title       = "AFT pipeline failed"
        description = "AFT pipeline <pipeline> FAILED at <time> (execution <execution>). https://<region>.console.aws.amazon.com/codesuite/codepipeline/pipelines/<pipeline>/executions/<execution>?region=<region>"
      }
    }) : "\"AFT pipeline <pipeline> FAILED at <time> (execution <execution>). https://<region>.console.aws.amazon.com/codesuite/codepipeline/pipelines/<pipeline>/executions/<execution>?region=<region>\""
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

########################################################################
# Weekly full run
########################################################################

resource "aws_cloudwatch_event_rule" "weekly_full_run" {
  name                = "${var.name_prefix}-weekly-full-run"
  description         = "Start every AFT customizations pipeline once a week, regardless of drift"
  schedule_expression = var.full_run_schedule_expression
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "weekly_full_run" {
  rule      = aws_cloudwatch_event_rule.weekly_full_run.name
  target_id = "full-run-lambda"
  arn       = module.full_run.lambda_function_arn
}
