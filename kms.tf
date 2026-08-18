########################################################################
# Encryption key
#
# A customer managed key is not optional here: EventBridge publishes the
# failure notifications directly to SNS, and a service principal can only
# do that if the key policy grants it - which rules out the AWS managed
# alias/aws/sns key, whose policy cannot be edited. The same key encrypts
# the probe pipeline's artifacts.
########################################################################

locals {
  create_kms_key = var.kms_key_arn == ""
  kms_key_arn    = local.create_kms_key ? aws_kms_key.this[0].arn : var.kms_key_arn
}

data "aws_iam_policy_document" "kms" {
  count = local.create_kms_key ? 1 : 0

  statement {
    sid       = "EnableIamUserPermissions"
    actions   = ["kms:*"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }

  statement {
    sid = "AllowEventBridgeToPublishEncryptedNotifications"
    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey*",
    ]
    resources = ["*"]

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
}

resource "aws_kms_key" "this" {
  count = local.create_kms_key ? 1 : 0

  description             = "AFT pipeline drift monitor notifications and pipeline artifacts"
  enable_key_rotation     = true
  deletion_window_in_days = var.kms_key_deletion_window_in_days
  policy                  = data.aws_iam_policy_document.kms[0].json
  tags                    = var.tags
}

resource "aws_kms_alias" "this" {
  count = local.create_kms_key ? 1 : 0

  name          = "alias/${var.name_prefix}"
  target_key_id = aws_kms_key.this[0].key_id
}
