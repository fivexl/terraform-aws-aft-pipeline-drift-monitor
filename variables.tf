variable "name_prefix" {
  description = "Prefix for every resource name created by this module."
  type        = string
  default     = "aft-pipeline-drift-monitor"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,40}$", var.name_prefix))
    error_message = "name_prefix must be lowercase alphanumeric with hyphens, 2-41 characters."
  }
}

variable "pipeline_name_pattern" {
  description = "Python regular expression the Lambdas use to select AFT customizations pipelines. The default matches AFT's own naming, `<account-id>-customizations-pipeline`."
  type        = string
  default     = "^\\d{12}-customizations-pipeline$"

  validation {
    condition     = length(var.pipeline_name_pattern) > 0
    error_message = "pipeline_name_pattern must not be empty."
  }
}

variable "failure_pipeline_name_suffix" {
  description = "Pipeline name suffix matched by the EventBridge failure rule, and used to scope the Lambdas' CodePipeline IAM permissions. Must be consistent with pipeline_name_pattern - a wrong value causes AccessDenied, not just missing alerts. An empty value is rejected: it would widen the IAM resource ARN and the EventBridge match to every CodePipeline in the account."
  type        = string
  default     = "-customizations-pipeline"

  validation {
    condition     = length(var.failure_pipeline_name_suffix) > 0
    error_message = "failure_pipeline_name_suffix must not be empty: an empty suffix widens the IAM pipeline ARN to arn:<partition>:codepipeline:*:<account>:* and makes the EventBridge failure rule match every pipeline in the account."
  }

  validation {
    condition     = can(regex("^[A-Za-z0-9._@-]+$", var.failure_pipeline_name_suffix))
    error_message = "failure_pipeline_name_suffix must contain only the characters CodePipeline allows in a pipeline name: A-Z a-z 0-9 . _ @ and -."
  }
}

variable "schedule_expression" {
  description = "Schedule for the daily drift check. Starts the revision probe pipeline, which resolves HEAD through the AFT CodeConnections connection and then invokes the drift detector."
  type        = string
  default     = "cron(0 2 * * ? *)"
}

variable "report_schedule_expression" {
  description = "Schedule for the status report. Set it a few hours after schedule_expression so the pipelines started by the drift check have finished."
  type        = string
  default     = "cron(0 8 * * ? *)"
}

variable "full_run_schedule_expression" {
  description = "Schedule for the weekly full run, which starts every AFT customizations pipeline regardless of drift. Defaults to Monday 06:00 UTC - after the daily drift check, and deliberately before report_schedule_expression, so Monday's report describes a full run that is still in flight."
  type        = string
  default     = "cron(0 6 ? * MON *)"
}

variable "detect_changes" {
  description = "Whether the revision probe pipeline also triggers on pushes to the customizations repositories, in addition to the daily schedule. Requires the CodeConnections connection to be able to create a webhook."
  type        = bool
  default     = true
}

variable "dry_run" {
  description = "Detect and report without starting any AFT pipeline. Applies to both the daily drift check and the weekly full run. Useful for the first few days in a new organisation."
  type        = bool
  default     = false
}

variable "max_pipelines_per_run" {
  description = "Maximum number of AFT pipelines to start in a single drift check. The remainder is deferred to the next run, which keeps CodeBuild concurrency and Terraform state contention under control."
  type        = number
  default     = 20

  validation {
    condition     = var.max_pipelines_per_run >= 1 && floor(var.max_pipelines_per_run) == var.max_pipelines_per_run
    error_message = "max_pipelines_per_run must be a whole number of at least 1."
  }
}

variable "full_run_max_pipelines_per_run" {
  description = "Maximum number of AFT pipelines to start in a single weekly full run. Defaults to max_pipelines_per_run, but the two are independent: the daily drift check only has to start pipelines that actually drifted, while the full run starts every account regardless, so the same cap can be too low to cover the whole estate weekly. The full run selects oldest-execution-first, so a cap below your account count rotates through every account over successive weeks rather than starving the same accounts - set this at or above your account count if you want every account re-applied every week. Set to -1 to reuse max_pipelines_per_run (the default)."
  type        = number
  default     = -1

  validation {
    condition     = var.full_run_max_pipelines_per_run == -1 || (var.full_run_max_pipelines_per_run >= 1 && floor(var.full_run_max_pipelines_per_run) == var.full_run_max_pipelines_per_run)
    error_message = "full_run_max_pipelines_per_run must be -1 (use max_pipelines_per_run) or a whole number of at least 1."
  }
}

variable "notify_on_drift" {
  description = "Publish an SNS summary for each drift check that found something to report - drifted, started, skipped, failing-on-HEAD or unstartable pipelines. Does not affect the scheduled status report or the weekly full run summary, which have their own notification behaviour, nor the EventBridge failure alerts, which are always published."
  type        = bool
  default     = true
}

variable "notify_status_report_when_clean" {
  description = "Publish the scheduled status report even when every pipeline is current - no failures and nothing behind HEAD. Defaults to false, so only an actionable report (something failed, or HEAD could not be resolved/was only partially resolved) is sent. Does not affect the drift check or the weekly full run summary, which have their own notification behaviour."
  type        = bool
  default     = false
}

variable "notify_full_run_when_clean" {
  description = "Publish the weekly full run summary even when nothing was started and nothing failed to start - every pipeline was already running, or there was simply nothing eligible. Defaults to false, so only an actionable summary (something started, something failed to start, a dry run, or no matching pipeline) is sent. Does not affect the drift check or the scheduled status report, which have their own notification behaviour."
  type        = bool
  default     = false
}

variable "create_sns_topic" {
  description = "Whether to create the notification topic. Ignored when sns_topic_arn is set - an existing topic always wins, so nothing is created. Set this to false only together with sns_topic_arn."
  type        = bool
  default     = true

  validation {
    condition     = var.create_sns_topic || var.sns_topic_arn != ""
    error_message = "Set create_sns_topic = true, or supply sns_topic_arn: the module has to have a topic to publish to."
  }
}

variable "sns_topic_arn" {
  description = "ARN of an existing SNS topic to publish to, in this account or another. Takes precedence over create_sns_topic. The topic policy must allow sns:Publish to this account or to the specific principals: the three Lambda roles and the pipeline_failed_target_role_arn output. An encrypted topic in another account is not supported - see the README."
  type        = string
  default     = ""
}

variable "kms_key_arn" {
  description = "ARN of an existing KMS key used for the SNS topic and the probe pipeline's artifacts. Leave empty to have the module create one. A supplied key must allow events.amazonaws.com to kms:Decrypt and kms:GenerateDataKey*, otherwise EventBridge cannot publish the failure notifications."
  type        = string
  default     = ""
}

variable "kms_key_deletion_window_in_days" {
  description = "Deletion window for the KMS key created by this module."
  type        = number
  default     = 30
}

variable "python_runtime" {
  description = "Lambda Python runtime."
  type        = string
  default     = "python3.14"
}

variable "lambda_ignore_source_code_hash" {
  description = "Passed straight through to terraform-aws-modules/lambda/aws for all three Lambda functions. Suppresses a spurious plan/apply mismatch on the first apply in a fresh working directory, where the deployment archive does not exist yet when the module computes source_code_hash. Real code changes are still deployed: this module's Lambda functions derive their aws_lambda_function.filename from the content of src/ on every plan, and that filename changing is what the AWS provider actually keys a redeploy on, independent of source_code_hash. Defaults to true, since there is no known downside to that in this module's configuration."
  type        = bool
  default     = true
}

variable "lambda_memory_size" {
  description = "Memory in MB for all three Lambda functions."
  type        = number
  default     = 512
}

variable "drift_detector_timeout" {
  description = "Timeout in seconds for the drift detector. It reads the last 10 executions of every AFT pipeline, so scale it with the number of vended accounts."
  type        = number
  default     = 600
}

variable "status_report_timeout" {
  description = "Timeout in seconds for the status report Lambda."
  type        = number
  default     = 300
}

variable "full_run_timeout" {
  description = "Timeout in seconds for the weekly full run Lambda. It reads the last 10 executions of every AFT pipeline before starting it, so scale it with the number of vended accounts."
  type        = number
  default     = 600
}

variable "log_retention_in_days" {
  description = "CloudWatch Logs retention for all three Lambda functions."
  type        = number
  default     = 30
}

variable "log_level" {
  description = "Python log level for all three Lambda functions."
  type        = string
  default     = "INFO"

  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR"], var.log_level)
    error_message = "log_level must be one of DEBUG, INFO, WARNING, ERROR."
  }
}

variable "artifact_bucket_name" {
  description = "Name of the S3 bucket for the revision probe pipeline's artifacts. Leave empty to derive it from name_prefix and the account id."
  type        = string
  default     = ""
}

variable "artifact_retention_days" {
  description = "Days before probe pipeline artifacts expire. They are only used to resolve commit ids, so they have no value after the run. Non-current versions expire one day later, so total retention is this plus one."
  type        = number
  default     = 7

  validation {
    condition     = var.artifact_retention_days >= 1
    error_message = "artifact_retention_days must be at least 1."
  }
}

variable "tags" {
  description = "Tags applied to every resource that supports them."
  type        = map(string)
  default     = {}
}

########################################################################
# Slack delivery via Amazon Q Developer in chat applications (AWS Chatbot)
########################################################################

variable "enable_chatbot" {
  description = "Subscribe a Slack channel to the notification topic through Amazon Q Developer in chat applications (AWS Chatbot). Requires the Slack workspace to have been authorized once by hand in the console, which is what produces slack_workspace_id."
  type        = bool
  default     = false

  validation {
    condition     = !var.enable_chatbot || (var.slack_workspace_id != "" && var.slack_channel_id != "")
    error_message = "enable_chatbot requires both slack_workspace_id and slack_channel_id."
  }
}

variable "slack_workspace_id" {
  description = "Slack workspace (team) id, as returned when you authorize the workspace in the Amazon Q Developer in chat applications console. Looks like T07EA123LEP."
  type        = string
  default     = ""
}

variable "slack_channel_id" {
  description = "Slack channel id the notifications are posted to. Looks like C07EZ1ABC23 - copy it from the channel details, not the channel name."
  type        = string
  default     = ""
}

variable "chatbot_iam_role_arn" {
  description = "ARN of an existing role for Chatbot to assume. Leave empty to have the module create a read-only one."
  type        = string
  default     = ""
}

variable "chatbot_guardrail_policy_arns" {
  description = "IAM policy ARNs applied as channel guardrails, capping what anyone can do through the channel. AWS applies AdministratorAccess when this is empty, so the default here is read-only instead."
  type        = list(string)
  default     = ["arn:aws:iam::aws:policy/ReadOnlyAccess"]

  validation {
    condition     = length(var.chatbot_guardrail_policy_arns) > 0
    error_message = "chatbot_guardrail_policy_arns must not be empty: an empty list makes AWS fall back to AdministratorAccess."
  }
}

variable "chatbot_logging_level" {
  description = "CloudWatch logging level for the Chatbot configuration: ERROR, INFO or NONE. ERROR is the default because NONE hides a message Chatbot rejects (e.g. wrong format) with nothing logged anywhere."
  type        = string
  default     = "ERROR"

  validation {
    condition     = contains(["ERROR", "INFO", "NONE"], var.chatbot_logging_level)
    error_message = "chatbot_logging_level must be one of ERROR, INFO, NONE."
  }
}
