# Read-only CloudWatch access for Grafana Cloud.
#
# Grafana's own instructions are to click this role together in the IAM
# console. It lives here instead because everything else this project
# runs on does, and a hand-made role is the one nobody remembers exists
# when the trial ends or the stack is rebuilt.
#
# Metrics, plus logs scoped to this project's own Lambda groups.
#
# Logs were left out at first on cost grounds and that was the wrong
# call. The failure this observability exists to catch was a Lambda dying
# at import, and the traceback saying so was sitting in CloudWatch while
# the pipeline was diagnosed from a status endpoint instead, because
# nobody could reach the logs. Being able to read them is the point.
#
# The cost is real but small and it is per query, not per hour: Logs
# Insights bills per gigabyte scanned, so an occasional search over
# 14 days of a five-minute cron costs a fraction of a cent. The way to
# make it expensive is to put a Logs Insights query on a dashboard that
# auto-refreshes, which re-scans on every tick. Don't.
#
# Worth knowing about the two different things Grafana calls AWS
# monitoring. The CloudWatch DATA SOURCE, which this role is for, calls
# GetMetricData only when a dashboard is open or an alert rule runs, so
# it costs a fraction of a cent as long as the rules stay few. The Cloud
# Provider Observability INTEGRATION is a scheduled scraper across whole
# namespaces, and that one runs whether anyone is looking or not. This
# role is scoped tightly enough to serve the first without inviting the
# second.

# This account's own id, so the log-group ARNs below are not a hardcoded
# number that quietly points at somebody else's account if this config is
# ever applied elsewhere.
data "aws_caller_identity" "current" {}

variable "grafana_cloud_account_id" {
  type        = string
  description = "Grafana Cloud's own AWS account, the one allowed to assume this role. Shown on the CloudWatch data source page under 'How to create an IAM role'."
  default     = "008923505280"
}

variable "grafana_external_id" {
  type        = string
  description = "Shared stack external ID from the same Grafana page. Not a secret, but it is what stops a confused deputy: without it, any other Grafana customer could ask Grafana to assume this role."
  default     = "1828431"
}

resource "aws_iam_role" "grafana_cloudwatch" {
  name        = "${var.project_name}-grafana-cloudwatch"
  description = "Read-only CloudWatch metrics for Grafana Cloud dashboards and alerts"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${var.grafana_cloud_account_id}:root" }
      Action    = "sts:AssumeRole"
      # The external ID is the whole security of this trust. Grafana
      # assumes roles for every customer from one account, so without
      # this condition the policy would read "any Grafana customer may
      # read our metrics".
      Condition = {
        StringEquals = { "sts:ExternalId" = var.grafana_external_id }
      }
    }]
  })
}

# Grafana's documented minimum for the CloudWatch data source, and
# nothing beyond it. ec2:DescribeRegions and tag:GetResources look out of
# place next to the cloudwatch actions: the first populates the region
# picker, the second lets a query filter by resource tag.
resource "aws_iam_role_policy" "grafana_cloudwatch" {
  name = "${var.project_name}-grafana-cloudwatch-read"
  role = aws_iam_role.grafana_cloudwatch.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "cloudwatch:ListMetrics",
        "cloudwatch:GetMetricData",
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:DescribeAlarmsForMetric",
        "ec2:DescribeRegions",
        "tag:GetResources",
      ]
      # None of these actions take a resource ARN: metrics are not
      # resources in IAM's sense, so scoping this further is not
      # possible. The read-only action list is the boundary.
      Resource = "*"
      },
      {
        # Logs, unlike metrics, do have ARNs, so this half is scoped to
        # this project's own Lambda groups. Grafana will not be able to
        # list or read anything else that ever lands in this account.
        Effect = "Allow"
        Action = [
          "logs:DescribeLogGroups",
          "logs:GetLogGroupFields",
          "logs:StartQuery",
          "logs:StopQuery",
          "logs:GetQueryResults",
          "logs:GetLogEvents",
          "logs:FilterLogEvents",
        ]
        Resource = [
          "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.project_name}-*",
          "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.project_name}-*:*",
        ]
    }]
  })
}

output "grafana_cloudwatch_role_arn" {
  description = "Paste into the CloudWatch data source's Assume Role ARN field in Grafana Cloud."
  value       = aws_iam_role.grafana_cloudwatch.arn
}
