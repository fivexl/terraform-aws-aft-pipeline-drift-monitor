# TFLint configuration.
#
# The AWS ruleset is what makes tflint worth running here: it validates instance
# types, ARN shapes and deprecated arguments against the real API, which
# `terraform validate` does not look at.
plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "aws" {
  enabled = true
  version = "0.44.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}

rule "terraform_required_version" {
  enabled = true
}

rule "terraform_required_providers" {
  enabled = true
}

# terraform-docs owns the ordering of variables and outputs in the README; the
# .tf files group them by purpose instead, which reads better than alphabetical.
rule "terraform_naming_convention" {
  enabled = true
}
