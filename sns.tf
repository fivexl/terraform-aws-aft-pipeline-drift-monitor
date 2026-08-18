########################################################################
# Notification topic
#
# Created only when the caller does not supply one. All three signals - drift
# check summary, pipeline failures and the status report - go to the same
# topic so a single subscription covers the module.
########################################################################

resource "aws_sns_topic" "this" {
  count = var.sns_topic_arn == "" ? 1 : 0

  name              = "${var.name_prefix}-notifications"
  display_name      = "AFT pipeline drift monitor"
  kms_master_key_id = local.kms_key_arn
  tags              = var.tags
}

data "aws_iam_policy_document" "sns_topic" {
  count = var.sns_topic_arn == "" ? 1 : 0

  statement {
    sid       = "AllowEventBridgePublish"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.this[0].arn]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
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
  count = var.sns_topic_arn == "" ? 1 : 0

  arn    = aws_sns_topic.this[0].arn
  policy = data.aws_iam_policy_document.sns_topic[0].json
}
