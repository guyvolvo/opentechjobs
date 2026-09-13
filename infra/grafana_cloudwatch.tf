# Read-only CloudWatch access for Grafana Cloud.
#
# Grafana's own instructions are to click this role together in the IAM
# console. It lives here instead because everything else this project
# runs on does, and a hand-made role is the one nobody remembers exists
# when the trial ends or the stack is rebuilt.
#
# Metrics only, deliberately. The policy carries no logs:StartQuery, so
# this role cannot run Logs Insights, which bills per gigabyte scanned
# and is the easiest way to turn a free dashboard into a real invoice.
# Lambda Errors, Duration and Throttles are what is worth watching here,
# and those are metrics.
#
# Worth knowing about the two different things Grafana calls AWS
# monitoring. The CloudWatch DATA SOURCE, which this role is for, calls
# GetMetricData only when a dashboard is open or an alert rule runs, so
# it costs a fraction of a cent as long as the rules stay few. The Cloud
# Provider Observability INTEGRATION is a scheduled scraper across whole
# namespaces, and that one runs whether anyone is looking or not. This
# role is scoped tightly enough to serve the first without inviting the
# second.

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
    }]
  })
}

output "grafana_cloudwatch_role_arn" {
  description = "Paste into the CloudWatch data source's Assume Role ARN field in Grafana Cloud."
  value       = aws_iam_role.grafana_cloudwatch.arn
}
