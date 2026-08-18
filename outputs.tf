output "sns_topic_arn" {
  description = "ARN of the topic every notification is published to - either the one supplied by the caller or the one this module created."
  value       = local.sns_topic_arn
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

output "daily_drift_check_rule_name" {
  description = "Name of the EventBridge rule that runs the daily drift check."
  value       = aws_cloudwatch_event_rule.daily_drift_check.name
}

output "status_report_rule_name" {
  description = "Name of the EventBridge rule that runs the status report."
  value       = aws_cloudwatch_event_rule.status_report.name
}

output "pipeline_failed_rule_name" {
  description = "Name of the EventBridge rule that forwards pipeline failures to SNS."
  value       = aws_cloudwatch_event_rule.pipeline_failed.name
}
