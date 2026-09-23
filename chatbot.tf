########################################################################
# Slack delivery via Amazon Q Developer in chat applications (AWS Chatbot)
#
# Subscribes a Slack channel to the module's SNS topic, so the drift
# summaries, failure alerts, status reports and weekly full-run summaries
# land in Slack without an email subscription in between.
#
# Prerequisite that Terraform cannot do for you: the Slack workspace has to
# be authorized once, by hand, in the Amazon Q Developer in chat applications
# console. That hands back the workspace (team) id this module needs. See
# https://docs.aws.amazon.com/chatbot/latest/adminguide/slack-setup.html
#
# The module's topic is encrypted with a customer-managed key, which is what
# Chatbot requires: the AWS-managed alias/aws/sns key cannot have its policy
# edited, so publishers could never be granted access to it. Chatbot itself
# needs no KMS permission - SNS decrypts before delivery - so only the
# publishers carry key grants, which they already do.
########################################################################

locals {
  create_chatbot_role = var.enable_chatbot && var.chatbot_iam_role_arn == ""
  chatbot_role_arn    = var.chatbot_iam_role_arn != "" ? var.chatbot_iam_role_arn : one(aws_iam_role.chatbot[*].arn)
}

data "aws_iam_policy_document" "chatbot_assume" {
  count = local.create_chatbot_role ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["chatbot.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "chatbot" {
  count = local.create_chatbot_role ? 1 : 0

  name               = "${var.name_prefix}-chatbot"
  description        = "Role Amazon Q Developer in chat applications assumes to render notifications"
  assume_role_policy = data.aws_iam_policy_document.chatbot_assume[0].json
  tags               = var.tags
}

# Enough to render an alarm or event in the channel, and nothing more. Channel
# guardrails cap what a user can do through chat; this caps the bot itself.
data "aws_iam_policy_document" "chatbot" {
  count = local.create_chatbot_role ? 1 : 0

  statement {
    sid = "ReadCloudWatchForNotificationRendering"
    actions = [
      "cloudwatch:Describe*",
      "cloudwatch:Get*",
      "cloudwatch:List*",
    ]
    resources = ["*"] # CloudWatch read actions do not support resource-level permissions
  }
}

resource "aws_iam_role_policy" "chatbot" {
  count = local.create_chatbot_role ? 1 : 0

  name   = "${var.name_prefix}-chatbot-notifications-only"
  role   = aws_iam_role.chatbot[0].id
  policy = data.aws_iam_policy_document.chatbot[0].json
}

resource "aws_chatbot_slack_channel_configuration" "this" {
  count = var.enable_chatbot ? 1 : 0

  configuration_name = "${var.name_prefix}-slack"
  slack_team_id      = var.slack_workspace_id
  slack_channel_id   = var.slack_channel_id
  iam_role_arn       = local.chatbot_role_arn
  sns_topic_arns     = [local.sns_topic_arn]

  # AWS applies AdministratorAccess when this is unset, which is not a default
  # a notification channel should carry.
  guardrail_policy_arns = var.chatbot_guardrail_policy_arns
  logging_level         = var.chatbot_logging_level

  tags = var.tags

  # A cross-input assertion, so a precondition rather than a validation on
  # enable_chatbot - see terraform_data.preflight in main.tf for why. This
  # resource only exists when enable_chatbot is true, which is exactly when the
  # two ids are required, so the check lands where it applies.
  lifecycle {
    precondition {
      condition     = var.slack_workspace_id != "" && var.slack_channel_id != ""
      error_message = "enable_chatbot requires both slack_workspace_id and slack_channel_id. The workspace id comes from authorizing the workspace by hand in the Amazon Q Developer in chat applications console; the channel id comes from the Slack channel's details, not its name."
    }
  }
}
