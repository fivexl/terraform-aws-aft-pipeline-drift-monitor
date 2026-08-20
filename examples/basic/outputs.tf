output "sns_topic_arn" {
  description = "Topic every drift summary, failure alert and status report is published to."
  value       = module.aft_pipeline_drift_monitor.sns_topic_arn
}

output "revision_probe_pipeline_name" {
  description = "Start this pipeline to run a drift check on demand."
  value       = module.aft_pipeline_drift_monitor.revision_probe_pipeline_name
}

output "full_run_function_name" {
  description = "Invoke this function to run every pipeline on demand, drift or not."
  value       = module.aft_pipeline_drift_monitor.full_run_function_name
}
