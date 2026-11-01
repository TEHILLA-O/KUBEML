output "cluster_name" {
  value = aws_eks_cluster.this.name
}

output "cluster_endpoint" {
  value = aws_eks_cluster.this.endpoint
}

output "repositories" {
  value = {
    platform = aws_ecr_repository.platform.repository_url
    serving  = aws_ecr_repository.serving.repository_url
    console  = aws_ecr_repository.console.repository_url
  }
}

output "cost_summary" {
  value = <<-EOT
    Optional AWS path. CI never applies this configuration.
    EKS control plane: one cluster, billed at the current AWS EKS hourly rate.
    Nodes: ${var.node_desired_size} x ${var.node_instance_type} (min ${var.node_min_size}, max ${var.node_max_size}).
    NAT gateway: ${var.enable_nat_gateway ? "one gateway plus data processing" : "disabled"}.
    ECR: three immutable repositories with scan on push.
    Destroy with an approved local terraform destroy. Do not destroy from a pull request.
  EOT
}
