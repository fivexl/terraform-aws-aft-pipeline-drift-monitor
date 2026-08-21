########################################################################
# AFT configuration lookups
#
# AFT publishes its VCS configuration to fixed SSM parameter paths in the
# AFT management account, so this module reads them instead of asking the
# caller to repeat values that already exist.
########################################################################

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

data "aws_ssm_parameter" "codeconnections_connection_arn" {
  name = "/aft/config/vcs/codeconnections-connection-arn"
}

data "aws_ssm_parameter" "global_customizations_repo_name" {
  name = "/aft/config/global-customizations/repo-name"
}

data "aws_ssm_parameter" "global_customizations_repo_branch" {
  name = "/aft/config/global-customizations/repo-branch"
}

data "aws_ssm_parameter" "account_customizations_repo_name" {
  name = "/aft/config/account-customizations/repo-name"
}

data "aws_ssm_parameter" "account_customizations_repo_branch" {
  name = "/aft/config/account-customizations/repo-branch"
}

locals {
  probe_pipeline_name = "${var.name_prefix}-revision-probe"

  # Repository names and branches are configuration, not secrets: unwrap them so
  # they can be used in for_each and read in a plan.
  connection_arn = nonsensitive(data.aws_ssm_parameter.codeconnections_connection_arn.value)

  # Action names deliberately mirror AFT's own source action names so the commit
  # ids reported by this pipeline compare 1:1 with the AFT pipelines'.
  probe_sources = [
    {
      name       = "aft-global-customizations"
      repository = nonsensitive(data.aws_ssm_parameter.global_customizations_repo_name.value)
      branch     = nonsensitive(data.aws_ssm_parameter.global_customizations_repo_branch.value)
    },
    {
      name       = "aft-account-customizations"
      repository = nonsensitive(data.aws_ssm_parameter.account_customizations_repo_name.value)
      branch     = nonsensitive(data.aws_ssm_parameter.account_customizations_repo_branch.value)
    },
  ]

  source_actions = [for source in local.probe_sources : source.name]

  # An existing topic wins: passing sns_topic_arn never creates one, whatever
  # create_sns_topic says.
  create_sns_topic = var.create_sns_topic && var.sns_topic_arn == ""
  sns_topic_arn    = var.sns_topic_arn != "" ? var.sns_topic_arn : one(aws_sns_topic.this[*].arn)

  # Region is wildcarded so the policies survive AFT being deployed in another
  # region; the account id keeps the scope to this account only.
  aft_customizations_pipeline_arn = "arn:${data.aws_partition.current.partition}:codepipeline:*:${data.aws_caller_identity.current.account_id}:*${var.failure_pipeline_name_suffix}"
  probe_pipeline_arn              = "arn:${data.aws_partition.current.partition}:codepipeline:*:${data.aws_caller_identity.current.account_id}:${local.probe_pipeline_name}"

  aft_pipeline_arns = [
    local.aft_customizations_pipeline_arn,
    local.probe_pipeline_arn,
  ]

  lambda_source_path = [
    {
      path = "${path.module}/src"
      # Deny everything, then re-admit only the handler modules. An exclusion
      # list silently ships whatever the next tool drops into src/ - a lint or
      # test cache is gitignored, so CI and a developer machine would then
      # produce different source_code_hash values and republish the functions
      # on alternating applies. Order matters: the last matching rule wins, so
      # the tests exclusion has to follow the .py admission.
      patterns = [
        "!.*",
        ".*\\.py",
        "!tests/.*",
      ]
    }
  ]

  lambda_environment = {
    PROBE_PIPELINE_NAME   = local.probe_pipeline_name
    PIPELINE_NAME_PATTERN = var.pipeline_name_pattern
    SNS_TOPIC_ARN         = local.sns_topic_arn
    SOURCE_ACTIONS        = join(",", local.source_actions)
    LOG_LEVEL             = var.log_level
    ENABLE_CHATBOT        = tostring(var.enable_chatbot)
  }
}

########################################################################
# Drift detector - invoked by the revision probe pipeline
########################################################################

module "drift_detector" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.2.1"

  function_name = "${var.name_prefix}-drift-detector"
  description   = "Starts AFT customizations pipelines whose last successful run used an older commit than HEAD"
  handler       = "drift_detector.lambda_handler"
  runtime       = var.python_runtime
  timeout       = var.drift_detector_timeout
  memory_size   = var.lambda_memory_size
  publish       = true

  source_path = local.lambda_source_path
  hash_extra  = "drift-detector"

  environment_variables = merge(local.lambda_environment, {
    DRY_RUN               = tostring(var.dry_run)
    MAX_PIPELINES_PER_RUN = tostring(var.max_pipelines_per_run)
    NOTIFY_ON_DRIFT       = tostring(var.notify_on_drift)
  })

  attach_policy_json = true
  policy_json        = data.aws_iam_policy_document.drift_detector.json

  cloudwatch_logs_retention_in_days = var.log_retention_in_days

  tags = var.tags
}

data "aws_iam_policy_document" "drift_detector" {
  statement {
    sid       = "DiscoverPipelines"
    actions   = ["codepipeline:ListPipelines"]
    resources = ["*"] # ListPipelines does not support resource-level permissions
  }

  statement {
    sid       = "InspectPipelineExecutions"
    actions   = ["codepipeline:ListPipelineExecutions"]
    resources = local.aft_pipeline_arns
  }

  statement {
    sid       = "ResolveHeadFromProbe"
    actions   = ["codepipeline:GetPipelineExecution"]
    resources = [local.probe_pipeline_arn]
  }

  statement {
    sid       = "RunAftPipelines"
    actions   = ["codepipeline:StartPipelineExecution"]
    resources = [local.aft_customizations_pipeline_arn]
  }

  statement {
    sid = "ReportJobResult"
    actions = [
      "codepipeline:PutJobFailureResult",
      "codepipeline:PutJobSuccessResult",
    ]
    resources = ["*"] # CodePipeline job ids are not addressable as ARNs
  }

  statement {
    sid       = "Notify"
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

########################################################################
# Status report - scheduled a few hours after the drift check
########################################################################

module "status_report" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.2.1"

  function_name = "${var.name_prefix}-status-report"
  description   = "Publishes an SNS report of AFT customizations pipeline outcomes and remaining drift"
  handler       = "status_report.lambda_handler"
  runtime       = var.python_runtime
  timeout       = var.status_report_timeout
  memory_size   = var.lambda_memory_size
  publish       = true

  source_path = local.lambda_source_path
  hash_extra  = "status-report"

  environment_variables = local.lambda_environment

  attach_policy_json = true
  policy_json        = data.aws_iam_policy_document.status_report.json

  cloudwatch_logs_retention_in_days = var.log_retention_in_days

  # The schedule targets the unqualified function ARN, so the permission has to
  # be attached there rather than to the published version.
  create_current_version_allowed_triggers = false

  allowed_triggers = {
    schedule = {
      principal  = "events.amazonaws.com"
      source_arn = aws_cloudwatch_event_rule.status_report.arn
    }
  }

  tags = var.tags
}

data "aws_iam_policy_document" "status_report" {
  statement {
    sid       = "DiscoverPipelines"
    actions   = ["codepipeline:ListPipelines"]
    resources = ["*"] # ListPipelines does not support resource-level permissions
  }

  statement {
    sid       = "InspectAftPipelines"
    actions   = ["codepipeline:ListPipelineExecutions"]
    resources = local.aft_pipeline_arns
  }

  statement {
    sid       = "Notify"
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

########################################################################
# Weekly full run - every pipeline, drift or not
#
# The drift detector only re-runs accounts whose commit is behind HEAD. This
# is the periodic baseline apply: it re-applies the customizations to every
# account, so drift inside an account (manual console changes) is corrected
# too, not just drift in the repository.
########################################################################

module "full_run" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.2.1"

  function_name = "${var.name_prefix}-full-run"
  description   = "Starts every AFT customizations pipeline on a weekly schedule, regardless of drift"
  handler       = "run_all.lambda_handler"
  runtime       = var.python_runtime
  timeout       = var.full_run_timeout
  memory_size   = var.lambda_memory_size
  publish       = true

  source_path = local.lambda_source_path
  hash_extra  = "full-run"

  environment_variables = merge(local.lambda_environment, {
    DRY_RUN               = tostring(var.dry_run)
    MAX_PIPELINES_PER_RUN = tostring(var.max_pipelines_per_run)
  })

  attach_policy_json = true
  policy_json        = data.aws_iam_policy_document.full_run.json

  cloudwatch_logs_retention_in_days = var.log_retention_in_days

  # The schedule targets the unqualified function ARN, so the permission has to
  # be attached there rather than to the published version.
  create_current_version_allowed_triggers = false

  allowed_triggers = {
    schedule = {
      principal  = "events.amazonaws.com"
      source_arn = aws_cloudwatch_event_rule.weekly_full_run.arn
    }
  }

  tags = var.tags
}

data "aws_iam_policy_document" "full_run" {
  statement {
    sid       = "DiscoverPipelines"
    actions   = ["codepipeline:ListPipelines"]
    resources = ["*"] # ListPipelines does not support resource-level permissions
  }

  statement {
    sid = "InspectAndRunAftPipelines"
    actions = [
      "codepipeline:ListPipelineExecutions",
      "codepipeline:StartPipelineExecution",
    ]
    resources = [local.aft_customizations_pipeline_arn]
  }

  statement {
    sid       = "Notify"
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
