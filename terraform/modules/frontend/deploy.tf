# Builds and uploads the SPA bundle, then invalidates CloudFront so a new
# deploy is visible immediately -- same pattern as
# terraform/modules/agent/s3.tf's build_agent_package (a scripts/*.sh
# local-exec triggered by a content hash), applied to the SPA instead of
# the agent's Python zip.

locals {
  # Explicit list of STATIC source files, not a directory glob. A glob
  # over frontend/src/** would be non-deterministic here: local_file.
  # runtime_config (below) writes runtime-config.json INTO this same
  # directory as part of this same apply, so fileset()'s result would
  # differ between plan and apply, and Terraform fails with "function
  # returned an inconsistent result". Naming the real
  # source files here avoids the race entirely; add new files to this
  # list if the SPA grows more source files later.
  spa_source_files = [
    "app.js",
    "auth.js",
    "agent-client.js",
    "index.html",
    "styles.css",
  ]
}

resource "null_resource" "build_spa" {
  triggers = {
    source_hash = sha256(join("", [
      for f in local.spa_source_files
      : filesha256("${path.module}/../../../frontend/src/${f}")
    ]))
    config_hash = sha256(local.runtime_config_json)
  }

  provisioner "local-exec" {
    command = "${path.module}/../../../scripts/build_spa.sh"
  }

  depends_on = [local_file.runtime_config]
}

# Runtime config the SPA bundle needs at load time (Cognito pool/client
# IDs, agent runtime ARN) -- written to a JSON file BEFORE the build runs
# (see scripts/build_spa.sh), so none of this needs to be hardcoded in the
# SPA's source or baked in at a different build step than the rest of the
# bundle.
# The SPA sends the Cognito ID token directly as a bearer token (see
# agent-client.js), so no Identity Pool or AWS credentials are involved.
locals {
  runtime_config_json = jsonencode({
    region                = data.aws_region.current.region
    userPoolId            = var.user_pool_id
    userPoolClientId      = var.spa_client_id
    agentRuntimeArn       = var.agent_runtime_arn
    agentRuntimeQualifier = "DEFAULT"
  })
}

data "aws_region" "current" {}

resource "local_file" "runtime_config" {
  filename = "${path.module}/../../../frontend/src/runtime-config.json"
  content  = local.runtime_config_json
}

data "archive_file" "spa_dist_check" {
  # Ensures the build has actually produced output before Terraform tries
  # to sync it -- archive_file will error clearly if build/spa-dist/
  # doesn't exist, rather than aws s3 sync silently uploading nothing.
  type        = "zip"
  source_dir  = "${path.module}/build/spa-dist"
  output_path = "${path.module}/build/spa-dist-check.zip"
  excludes    = ["__pycache__"]

  depends_on = [null_resource.build_spa, local_file.runtime_config]
}

resource "null_resource" "upload_spa" {
  triggers = {
    dist_hash = data.archive_file.spa_dist_check.output_md5
  }

  provisioner "local-exec" {
    command = "aws s3 sync ${path.module}/build/spa-dist s3://${aws_s3_bucket.spa.id} --delete --region ${data.aws_region.current.region}"
  }

  depends_on = [aws_s3_bucket_policy.spa, data.archive_file.spa_dist_check]
}

resource "null_resource" "invalidate_cloudfront" {
  triggers = {
    dist_hash = data.archive_file.spa_dist_check.output_md5
  }

  provisioner "local-exec" {
    command = "aws cloudfront create-invalidation --distribution-id ${aws_cloudfront_distribution.spa.id} --paths '/*'"
  }

  depends_on = [null_resource.upload_spa]
}
