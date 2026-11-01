# Optional AWS path

This module is not used by the local demo and is not applied by CI.

It provisions one VPC, public and private subnets, an EKS control plane, a managed node group, and three immutable ECR repositories with scan on push. NAT is enabled by default because private nodes need it to pull images. Set `enable_nat_gateway = false` only if you have another egress path.

Before `terraform plan`, read `terraform output` expectations in `outputs.tf`. The cost summary counts:

- one EKS control plane
- two `t3.large` nodes by default
- one NAT gateway when enabled

Copy `backend.tf.example` to an uncommitted `backend.tf` when a team needs locked remote state. Do not commit state or credentials.

```powershell
cd terraform\aws
terraform fmt
terraform init -backend=false
terraform validate
terraform plan
```

`terraform apply` and `terraform destroy` are manual, reviewed operations. Pull requests do not run them. GitHub deployment to a private cluster needs a self-hosted runner or a network path you control. A GitHub-hosted runner cannot see a kind cluster on your laptop, so the CI kind job creates its own ephemeral cluster instead of reporting a local deployment.
