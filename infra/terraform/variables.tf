variable "aws_region" {
  description = "AWS region to deploy the EC2 instance into."
  type        = string
  default     = "ap-south-1"
}

variable "project_name" {
  description = "Prefix used for resource names."
  type        = string
  default     = "cricket"
}

variable "key_pair_name" {
  description = "Existing AWS EC2 key pair name used for SSH access."
  type        = string
}

variable "ssh_allowed_cidr" {
  description = "CIDR block allowed to SSH to the server."
  type        = string
  default     = "0.0.0.0/0"
}

variable "root_volume_size_gb" {
  description = "Root EBS volume size in GB."
  type        = number
  default     = 30
}

variable "app_directory" {
  description = "Directory on the server where the Next.js app will live."
  type        = string
  default     = "/var/www/cricket_ui"
}

variable "app_port" {
  description = "Port used by the Next.js production server."
  type        = number
  default     = 3000
}

variable "node_major_version" {
  description = "Node.js major version to install from NodeSource."
  type        = number
  default     = 20
}
