########################################################################
# Revision probe pipeline
#
# There is no CodeConnections API that returns the current commit of a
# connected repository, so this pipeline borrows CodePipeline's own
# resolution: it points the existing AFT connection at the same two
# customizations repositories, and CodePipeline reports the commit it
# resolved as the execution's source revision. The drift detector then
# compares that commit against what every AFT pipeline last applied.
########################################################################

resource "aws_codepipeline" "revision_probe" {
  name          = local.probe_pipeline_name
  role_arn      = aws_iam_role.probe_pipeline.arn
  pipeline_type = "V2"

  # Stage locking serialises the Detect-Drift invocations, so two probe runs can
  # never both call StartPipelineExecution on the same AFT pipeline. PARALLEL or
  # QUEUED would remove that mutex.
  execution_mode = "SUPERSEDED"

  artifact_store {
    location = aws_s3_bucket.artifacts.id
    type     = "S3"

    encryption_key {
      id   = local.kms_key_arn
      type = "KMS"
    }
  }

  stage {
    name = "Source"

    dynamic "action" {
      for_each = local.probe_sources

      content {
        name             = action.value.name
        category         = "Source"
        owner            = "AWS"
        provider         = "CodeStarSourceConnection"
        version          = "1"
        output_artifacts = [action.value.name]

        configuration = {
          ConnectionArn        = local.connection_arn
          FullRepositoryId     = action.value.repository
          BranchName           = action.value.branch
          DetectChanges        = var.detect_changes
          OutputArtifactFormat = "CODE_ZIP"
        }
      }
    }
  }

  stage {
    name = "Detect-Drift"

    action {
      name     = "Detect-Drift"
      category = "Invoke"
      owner    = "AWS"
      provider = "Lambda"
      version  = "1"

      # The source stage's artifacts are consumed purely for their metadata:
      # CodePipeline reports each one's resolved commit id in the job event as
      # inputArtifacts[].revision, which is how the detector learns HEAD. AWS
      # omits pipelineContext from Lambda action events, so without this wiring
      # there is nothing in the event tying the invocation to its own execution.
      input_artifacts = local.source_actions

      configuration = {
        FunctionName = module.drift_detector.lambda_function_name
      }
    }
  }

  tags = var.tags
}

########################################################################
# Pipeline role
########################################################################

data "aws_iam_policy_document" "probe_pipeline_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["codepipeline.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "probe_pipeline" {
  name               = "${var.name_prefix}-revision-probe-pipeline"
  description        = "Role for the AFT revision probe pipeline"
  assume_role_policy = data.aws_iam_policy_document.probe_pipeline_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "probe_pipeline" {
  statement {
    sid = "ArtifactBucket"
    actions = [
      "s3:GetBucketAcl",
      "s3:GetBucketLocation",
      "s3:GetBucketVersioning",
    ]
    resources = [aws_s3_bucket.artifacts.arn]
  }

  # An object-level grant cannot avoid the key wildcard: CodePipeline generates the
  # artifact keys. Scope is one dedicated, module-owned bucket.
  #tfsec:ignore:AVD-AWS-0057
  statement {
    sid = "ArtifactObjects"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
      "s3:PutObjectAcl",
    ]
    resources = ["${aws_s3_bucket.artifacts.arn}/*"]
  }

  statement {
    sid = "UseAftConnection"
    # codestar-connections is the legacy namespace of the same permission; both
    # are granted so the module works whichever namespace AFT's connection uses.
    actions = [
      "codeconnections:UseConnection",
      "codestar-connections:UseConnection",
    ]
    resources = [local.connection_arn]
  }

  statement {
    sid       = "InvokeDriftDetector"
    actions   = ["lambda:InvokeFunction"]
    resources = [module.drift_detector.lambda_function_arn]
  }

  statement {
    sid       = "ListFunctions"
    actions   = ["lambda:ListFunctions"]
    resources = ["*"] # required by the CodePipeline Lambda action, not resource-scoped
  }

  statement {
    sid = "UseEncryptionKey"
    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey",
    ]
    resources = [local.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "probe_pipeline" {
  name   = "${var.name_prefix}-revision-probe-pipeline"
  role   = aws_iam_role.probe_pipeline.id
  policy = data.aws_iam_policy_document.probe_pipeline.json
}

########################################################################
# Artifact bucket
#
# Holds nothing but zipped copies of the customizations repositories, which
# are only fetched so CodePipeline reports the commit it resolved.
########################################################################

#tfsec:ignore:AVD-AWS-0089 - access logging would need a second permanent bucket to log a bucket that only ever holds ephemeral repo zips
resource "aws_s3_bucket" "artifacts" {
  bucket        = var.artifact_bucket_name != "" ? var.artifact_bucket_name : "${var.name_prefix}-artifacts-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
  tags          = var.tags
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = local.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  depends_on = [aws_s3_bucket_versioning.artifacts]

  rule {
    id     = "expire-probe-artifacts"
    status = "Enabled"

    filter {}

    expiration {
      days = var.artifact_retention_days
    }

    # Counts from the day the expiration action made the version noncurrent, so
    # keep it at 1 - otherwise total retention is artifact_retention_days * 2.
    noncurrent_version_expiration {
      noncurrent_days = 1
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 3
    }
  }
}

data "aws_iam_policy_document" "artifacts" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.artifacts.arn,
      "${aws_s3_bucket.artifacts.arn}/*",
    ]

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  policy = data.aws_iam_policy_document.artifacts.json

  depends_on = [aws_s3_bucket_public_access_block.artifacts]
}
