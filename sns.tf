########################################################################
# Notification topic
#
# Created when create_sns_topic is true and no sns_topic_arn is supplied. All
# four signals - drift check summary, pipeline failures, the status report and
# the weekly full run - go to the same topic, so a single subscription covers
# the whole module.
########################################################################

resource "aws_sns_topic" "this" {
  count = local.create_sns_topic ? 1 : 0

  name              = "${var.name_prefix}-notifications"
  display_name      = "AFT pipeline drift monitor"
  kms_master_key_id = local.kms_key_arn
  tags              = var.tags
}

data "aws_iam_policy_document" "sns_topic" {
  count = local.create_sns_topic ? 1 : 0

  # Unconditioned for the same reason as the KMS grant: this is the same
  # EventBridge call path, and AWS's documented EventBridge-to-SNS statement
  # carries no source condition. A condition that is never populated denies the
  # publish silently, which would disable failure alerting altogether.
  statement {
    sid       = "AllowEventBridgePublish"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.this[0].arn]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }

  statement {
    sid = "AllowAccountOwner"
    actions = [
      "sns:AddPermission",
      "sns:DeleteTopic",
      "sns:GetTopicAttributes",
      "sns:ListSubscriptionsByTopic",
      "sns:Publish",
      "sns:RemovePermission",
      "sns:SetTopicAttributes",
      "sns:Subscribe",
    ]
    resources = [aws_sns_topic.this[0].arn]

    principals {
      type        = "AWS"
      identifiers = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "this" {
  count = local.create_sns_topic ? 1 : 0

  arn    = aws_sns_topic.this[0].arn
  policy = data.aws_iam_policy_document.sns_topic[0].json
}
