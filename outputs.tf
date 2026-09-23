output "sns_topic_arn" {
  description = "ARN of the topic every notification is published to - either the one supplied by the caller or the one this module created."
  value       = local.sns_topic_arn
}

output "notification_publisher_role_arns" {
  description = "Every principal this module publishes to the notification topic with, keyed by what it is. When sns_topic_arn points at a topic this module does not manage, its owner has to authorize exactly these - so they are output by name rather than left to be guessed from the generated role names or discovered as a runtime AccessDenied. Granting sns:Publish to this account covers all of them at once."
  value = {
    drift_detector         = module.drift_detector.lambda_role_arn
    status_report          = module.status_report.lambda_role_arn
    full_run               = module.full_run.lambda_role_arn
    pipeline_failed_events = aws_iam_role.eventbridge_sns.arn
  }
}

output "kms_key_arn" {
  description = "ARN of the key encrypting the notification topic and the probe pipeline's artifacts - either the one supplied via kms_key_arn or the one this module created. A cross-account topic owner needs this to know which key its publishers decrypt with."
  value       = local.kms_key_arn
}

output "revision_probe_pipeline_name" {
  description = "Name of the pipeline that resolves HEAD of the customizations repositories through the AFT CodeConnections connection."
  value       = aws_codepipeline.revision_probe.name
}

output "revision_probe_pipeline_arn" {
  description = "ARN of the revision probe pipeline."
  value       = aws_codepipeline.revision_probe.arn
}

output "artifact_bucket_name" {
  description = "Name of the S3 bucket holding the revision probe pipeline's artifacts."
  value       = aws_s3_bucket.artifacts.id
}

output "drift_detector_function_name" {
  description = "Name of the drift detector Lambda function."
  value       = module.drift_detector.lambda_function_name
}

output "drift_detector_function_arn" {
  description = "ARN of the drift detector Lambda function."
  value       = module.drift_detector.lambda_function_arn
}

output "status_report_function_name" {
  description = "Name of the status report Lambda function."
  value       = module.status_report.lambda_function_name
}

output "status_report_function_arn" {
  description = "ARN of the status report Lambda function."
  value       = module.status_report.lambda_function_arn
}

output "full_run_function_name" {
  description = "Name of the weekly full run Lambda function."
  value       = module.full_run.lambda_function_name
}

output "full_run_function_arn" {
  description = "ARN of the weekly full run Lambda function."
  value       = module.full_run.lambda_function_arn
}

output "weekly_full_run_rule_name" {
  description = "Name of the EventBridge rule that re-applies the customizations to every AFT-managed account weekly."
  value       = aws_cloudwatch_event_rule.weekly_full_run.name
}

output "daily_drift_check_rule_name" {
  description = "Name of the EventBridge rule that runs the daily drift check."
  value       = aws_cloudwatch_event_rule.daily_drift_check.name
}

output "status_report_rule_name" {
  description = "Name of the EventBridge rule that runs the status report."
  value       = aws_cloudwatch_event_rule.status_report.name
}

output "pipeline_failed_target_role_arn" {
  description = "Role EventBridge assumes to publish failure notifications. A topic in another account must allow this role (or this account) to sns:Publish."
  value       = aws_iam_role.eventbridge_sns.arn
}

output "pipeline_failed_rule_name" {
  description = "Name of the EventBridge rule that forwards pipeline failures to SNS."
  value       = aws_cloudwatch_event_rule.pipeline_failed.name
}

output "chatbot_configuration_arn" {
  description = "ARN of the Chatbot Slack channel configuration, or null when enable_chatbot is false."
  value       = one(aws_chatbot_slack_channel_configuration.this[*].chat_configuration_arn)
}

output "chatbot_iam_role_arn" {
  description = "Role Chatbot assumes - the one supplied via chatbot_iam_role_arn, the one this module created, or null when enable_chatbot is false."
  value       = var.enable_chatbot ? local.chatbot_role_arn : null
}
