output "instance_id" {
  description = "EC2 instance ID."
  value       = aws_instance.app.id
}

output "instance_public_ip" {
  description = "Elastic IP attached to the EC2 instance."
  value       = aws_eip.app.public_ip
}

output "ssh_command" {
  description = "SSH command template. Replace the key file path before use."
  value       = "ssh -i /path/to/your-key.pem ubuntu@${aws_eip.app.public_ip}"
}
