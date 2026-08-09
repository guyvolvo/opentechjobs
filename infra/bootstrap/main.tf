# One-time bootstrap: creates the S3 bucket that holds Terraform state for
# the real infra/ config. Chicken-and-egg problem -- Terraform can't manage
# the bucket it stores its own state in before that bucket exists -- so
# this lives in its own tiny local-state config, applied once by hand:
#
#   cd infra/bootstrap
#   terraform init
#   terraform apply
#
# After this exists, infra/ points its backend at the bucket this creates
# and is never touched again. Not part of the GitHub Actions deploy flow
# on purpose: state-backend bootstrapping is a rare, deliberate action, not
# something that should be able to happen from a CI trigger.

terraform {
  required_version = ">= 1.10.0" # >=1.10 for native S3 state locking, no DynamoDB table needed
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

variable "aws_region" {
  type    = string
  default = "il-central-1" # AWS Tel Aviv region -- correct home for an Israeli-jobs project's data
}

variable "state_bucket_name" {
  type        = string
  description = "Globally-unique S3 bucket name for Terraform state. S3 bucket names are a shared global namespace, so the default here WILL collide -- set your own."
  default     = "iljobs-tfstate-CHANGE-ME"
}

provider "aws" {
  region = var.aws_region
}

resource "aws_s3_bucket" "tfstate" {
  bucket = var.state_bucket_name

  lifecycle {
    prevent_destroy = true # state bucket: never let a stray `terraform destroy` take this out
  }
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled" # recovers a state file if a bad apply overwrites it
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

output "state_bucket_name" {
  value = aws_s3_bucket.tfstate.bucket
}
