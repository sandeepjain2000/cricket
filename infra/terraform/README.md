# Terraform EC2 Setup

This Terraform config creates:

- one Ubuntu EC2 instance sized as `t3.medium`
- one Elastic IP for a fixed public IP address
- one security group with ports `22`, `80`, `443`, and `3000`
- bootstrapping for `Node.js`, `pm2`, and `nginx`

## Before you apply

1. Create or reuse an EC2 key pair in AWS.
2. Install Terraform locally.
3. Configure AWS credentials, for example with `aws configure`.
4. Copy `terraform.tfvars.example` to `terraform.tfvars` and set your key pair name.

## Deploy

```bash
cd infra/terraform
terraform init
terraform plan
terraform apply
```

After apply, Terraform will output:

- the EC2 instance ID
- the fixed public IP
- an SSH command template

## After the server is up

SSH to the server, upload your app into `/var/www/cricket_ui`, then run:

```bash
cd /var/www/cricket_ui
npm install
npm run build
pm2 start npm --name cricket-ui -- start
pm2 save
```

Your app will then be reachable at:

```text
http://<elastic-ip>
```

## Notes

- The root EBS volume is set with `delete_on_termination = false` so you do not lose the attached disk by accident when destroying the EC2 instance.
- Keep your SQLite database on persistent storage and back it up regularly.
- For better security, change `ssh_allowed_cidr` from `0.0.0.0/0` to your own public IP range.
