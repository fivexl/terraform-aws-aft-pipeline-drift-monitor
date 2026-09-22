terraform {
  # 1.6.1 is the floor of the management-AFT stacks that consume this module.
  # Nothing here needs more: the two cross-variable assertions that used to
  # require 1.9 are resource preconditions (available since 1.2), and the newest
  # feature in use is terraform_data (1.4). CI validates on exactly this version,
  # so the floor is tested rather than asserted.
  required_version = ">= 1.6.1"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.28"
    }
  }
}
