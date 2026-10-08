data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# S3 bucket + deployment zip for the agent, using AgentCore Runtime's
# "direct code deployment" mode (aws_bedrockagentcore_agent_runtime with
# code_configuration) -- no container, no ECR, no Docker, no CodeBuild.
# AgentCore Runtime unzips this package and runs agent.py directly on a
# managed Python 3.13 arm64 execution environment.
#
# Dependencies are vendored into build/vendor/ by scripts/build_agent_zip.sh
# (pip install --platform manylinux2014_aarch64 --only-binary=:all: ...),
# then zipped together with agent/ source. This runs from Terraform via
# null_resource/local-exec, entirely on this machine, no Docker required.

resource "aws_s3_bucket" "agent_code" {
  bucket_prefix = "${var.name_prefix}-agent-code-"
  force_destroy = true

  tags = {
    Name    = "${var.name_prefix}-agent-code"
    Purpose = "Direct code deployment package for AgentCore Runtime"
  }
}

resource "aws_s3_bucket_public_access_block" "agent_code" {
  bucket = aws_s3_bucket.agent_code.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "agent_code" {
  bucket = aws_s3_bucket.agent_code.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "agent_code" {
  bucket = aws_s3_bucket.agent_code.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Builds build/agent-package/ (agent/*.py + vendored arm64 wheels for
# requirements.txt) then zips it. Re-runs whenever agent/ source or
# requirements.txt changes (content hash in triggers).
resource "null_resource" "build_agent_package" {
  triggers = {
    requirements_hash = filesha256("${path.module}/../../../agent/requirements.txt")
    agent_py_hash     = filesha256("${path.module}/../../../agent/agent.py")
  }

  provisioner "local-exec" {
    command = "${path.module}/../../../scripts/build_agent_zip.sh"
  }
}

data "archive_file" "agent_code" {
  type        = "zip"
  source_dir  = "${path.module}/build/agent-package"
  output_path = "${path.module}/build/agent-code.zip"
  excludes    = ["__pycache__"]

  depends_on = [null_resource.build_agent_package]
}

resource "aws_s3_object" "agent_code" {
  bucket = aws_s3_bucket.agent_code.id
  key    = "agent-code-${data.archive_file.agent_code.output_md5}.zip"
  source = data.archive_file.agent_code.output_path
  etag   = data.archive_file.agent_code.output_md5

  tags = {
    Name = "agent-code"
    MD5  = data.archive_file.agent_code.output_md5
  }
}
