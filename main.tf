########################################################################
# AFT configuration lookups
#
# AFT publishes its VCS configuration to fixed SSM parameter paths in the
# AFT management account, so this module reads them instead of asking the
# caller to repeat values that already exist.
########################################################################

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

data "aws_region" "current" {}

data "aws_ssm_parameter" "codeconnections_connection_arn" {
  name = "/aft/config/vcs/codeconnections-connection-arn"
}

# AFT's own version, which it publishes from 1.20.0 onward at least. Read purely
# to assert the >= 1.21.0 floor below at plan time.
data "aws_ssm_parameter" "aft_version" {
  name = "/aft/config/aft/version"
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

  # Scoped to the provider's region. Every client and resource this module talks
  # to lives in the AFT home region it is deployed into, so a region wildcard
  # only widened the grant to matching pipelines in every region of the account.
  aft_customizations_pipeline_arn = "arn:${data.aws_partition.current.partition}:codepipeline:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:*${var.failure_pipeline_name_suffix}"
  probe_pipeline_arn              = "arn:${data.aws_partition.current.partition}:codepipeline:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:${local.probe_pipeline_name}"

  aft_pipeline_arns = [
    local.aft_customizations_pipeline_arn,
    local.probe_pipeline_arn,
  ]

  # AFT creates this state machine with a fixed name in the AFT management
  # account, in the AFT home region - the same account and region this module is
  # deployed into, so it is derivable rather than an input.
  aft_invoke_customizations_arn = "arn:${data.aws_partition.current.partition}:states:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:stateMachine:aft-invoke-customizations"

  # `bypass_steps` requires AFT >= 1.21.0. Below that, the state machine's
  # `Check Bypass` choice does not exist and every invocation falls through to
  # `Invoke Provisioning Framework` - a DISTRIBUTED Map running the full
  # provisioning framework per account, which is far heavier than the
  # customizations re-run this module is asking for, and silently so. The check
  # is fail-OPEN on an unparseable version string: it exists to catch a genuine
  # 1.20.x install, not to block a version format AFT has not used yet.
  aft_version       = nonsensitive(data.aws_ssm_parameter.aft_version.value)
  aft_version_parts = try([for part in slice(split(".", local.aft_version), 0, 2) : tonumber(part)], [])
  aft_version_too_old = length(local.aft_version_parts) == 2 && (
    local.aft_version_parts[0] < 1 ||
    (local.aft_version_parts[0] == 1 && local.aft_version_parts[1] < 21)
  )

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
    PROBE_PIPELINE_NAME          = local.probe_pipeline_name
    PIPELINE_NAME_PATTERN        = var.pipeline_name_pattern
    SNS_TOPIC_ARN                = local.sns_topic_arn
    SOURCE_ACTIONS               = join(",", local.source_actions)
    AFT_INVOKE_STATE_MACHINE_ARN = local.aft_invoke_customizations_arn
    NAME_PREFIX                  = var.name_prefix
    LOG_LEVEL                    = var.log_level
    ENABLE_CHATBOT               = tostring(var.enable_chatbot)
  }
}

########################################################################
# Preflight checks
#
# Every assertion here is about more than one input, which is why it is a
# precondition rather than a variable validation: a validation that references
# another variable requires Terraform >= 1.9, and this module supports 1.6.1 (the
# floor of the management-AFT stacks that consume it). Preconditions have been
# available since 1.2 and produce the same plan-time failure.
########################################################################

resource "terraform_data" "preflight" {
  input = local.aft_version

  lifecycle {
    # The module invokes aft-invoke-customizations with `bypass_steps`, introduced
    # in AFT 1.21.0. Asserted at plan time rather than at runtime: a Lambda
    # discovering it at 02:00 has already lost the check, and there is
    # deliberately no fallback - see the aft_version_too_old local for what the
    # fallback would actually do.
    precondition {
      condition     = !local.aft_version_too_old
      error_message = "This module requires AFT >= 1.21.0, but /aft/config/aft/version reports ${local.aft_version}. It re-runs customizations through the aft-invoke-customizations state machine with bypass_steps = [\"provisioning_bootstrap\"], which AFT introduced in 1.21.0. On an older AFT the state machine has no Check Bypass state, so every invocation would instead run the FULL provisioning framework for every targeted account - far heavier than a customizations re-run, with no error to tell you. Upgrade AFT to 1.21.0 or later."
    }

    precondition {
      condition     = var.create_sns_topic || var.sns_topic_arn != ""
      error_message = "Set create_sns_topic = true, or supply sns_topic_arn: the module has to have a topic to publish to."
    }
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

  # No alias and no version-qualified invoke: EventBridge and CodePipeline both
  # target the unqualified function ARN, so publishing would only accumulate
  # immutable versions nothing can roll back to.
  publish = false

  source_path = local.lambda_source_path
  hash_extra  = "drift-detector"

  # terraform-aws-modules/lambda/aws computes source_code_hash from fileexists() on
  # the packaged archive. On the very first apply in a fresh working directory that
  # archive does not exist yet when the hash is computed, which can make plan and
  # apply disagree and require running apply twice. ignore_source_code_hash works
  # around it by skipping that hash entirely; real code changes still redeploy via
  # filename, which source_path derives from the content of src/ on every plan.
  ignore_source_code_hash = var.lambda_ignore_source_code_hash

  environment_variables = merge(local.lambda_environment, {
    DRY_RUN         = tostring(var.dry_run)
    NOTIFY_ON_DRIFT = tostring(var.notify_on_drift)
  })

  attach_policy_json = true
  policy_json        = data.aws_iam_policy_document.drift_detector.json

  cloudwatch_logs_retention_in_days = var.log_retention_in_days
  cloudwatch_logs_kms_key_id        = var.cloudwatch_logs_kms_key_id != "" ? var.cloudwatch_logs_kms_key_id : null

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
    # Reads the source action names AFT actually configured, so a rename on
    # AFT's side fails the check instead of marking every pipeline drifted.
    sid       = "InspectAftPipelineDefinition"
    actions   = ["codepipeline:GetPipeline"]
    resources = [local.aft_customizations_pipeline_arn]
  }

  statement {
    # Re-running an account goes through AFT's own state machine, which owns the
    # concurrency budget and the completion controls. That is why this role has
    # no codepipeline:StartPipelineExecution: a direct start would bypass both.
    sid       = "ReRunCustomizationsThroughAft"
    actions   = ["states:StartExecution"]
    resources = [local.aft_invoke_customizations_arn]
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

  # No alias and no version-qualified invoke: EventBridge and CodePipeline both
  # target the unqualified function ARN, so publishing would only accumulate
  # immutable versions nothing can roll back to.
  publish = false

  source_path = local.lambda_source_path
  hash_extra  = "status-report"

  # See drift_detector's ignore_source_code_hash comment above - same module,
  # same first-apply plan/apply mismatch, same fix.
  ignore_source_code_hash = var.lambda_ignore_source_code_hash

  environment_variables = merge(local.lambda_environment, {
    NOTIFY_WHEN_CLEAN = tostring(var.notify_status_report_when_clean)
  })

  attach_policy_json = true
  policy_json        = data.aws_iam_policy_document.status_report.json

  cloudwatch_logs_retention_in_days = var.log_retention_in_days
  cloudwatch_logs_kms_key_id        = var.cloudwatch_logs_kms_key_id != "" ? var.cloudwatch_logs_kms_key_id : null

  # The schedule targets the unqualified function ARN, so the permission belongs
  # there. Kept explicit even though publish = false leaves no version to attach
  # to, so re-enabling publish cannot silently move the permission.
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

  # No alias and no version-qualified invoke: EventBridge and CodePipeline both
  # target the unqualified function ARN, so publishing would only accumulate
  # immutable versions nothing can roll back to.
  publish = false

  source_path = local.lambda_source_path
  hash_extra  = "full-run"

  # See drift_detector's ignore_source_code_hash comment above - same module,
  # same first-apply plan/apply mismatch, same fix.
  ignore_source_code_hash = var.lambda_ignore_source_code_hash

  environment_variables = merge(local.lambda_environment, {
    DRY_RUN           = tostring(var.dry_run)
    NOTIFY_WHEN_CLEAN = tostring(var.notify_full_run_when_clean)
  })

  attach_policy_json = true
  policy_json        = data.aws_iam_policy_document.full_run.json

  cloudwatch_logs_retention_in_days = var.log_retention_in_days
  cloudwatch_logs_kms_key_id        = var.cloudwatch_logs_kms_key_id != "" ? var.cloudwatch_logs_kms_key_id : null

  # The schedule targets the unqualified function ARN, so the permission belongs
  # there. Kept explicit even though publish = false leaves no version to attach
  # to, so re-enabling publish cannot silently move the permission.
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
    # The weekly run inspects nothing: it hands AFT a single {"type": "all"}
    # selector and lets AFT resolve the account list from its metadata table, so
    # this role needs no CodePipeline access at all.
    sid       = "ReRunEveryAccountThroughAft"
    actions   = ["states:StartExecution"]
    resources = [local.aft_invoke_customizations_arn]
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
