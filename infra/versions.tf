terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }

  # Points at the bucket infra/bootstrap/ created. Fill in `bucket` after
  # running the bootstrap once -- Terraform backend blocks can't use
  # variables, so this really does need the literal name.
  backend "s3" {
    bucket       = "iljobs-tfstate-CHANGE-ME"
    key          = "iljobs/terraform.tfstate"
    region       = "il-central-1"
    encrypt      = true
    use_lockfile = true # native S3 locking (Terraform >=1.10) -- no DynamoDB table to pay for
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      project   = var.project_name
      managedby = "terraform"
    }
  }
}
