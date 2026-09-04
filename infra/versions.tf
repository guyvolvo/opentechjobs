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

  # Points at the bucket infra/bootstrap/ created. Backend blocks can't
  # use variables, so this needs the literal name.
  backend "s3" {
    bucket       = "iljobs-tfstate-876913698688"
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

# ACM certs for CloudFront must be issued in us-east-1 regardless of the
# distribution's own region -- a hard AWS constraint. Named generically,
# not cert-specific, since a future CloudFront WAF ACL needs the same thing.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
  default_tags {
    tags = {
      project   = var.project_name
      managedby = "terraform"
    }
  }
}
