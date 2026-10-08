// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// Bundles the SPA's JS + copies static assets into ../build/spa-dist
// (referenced by terraform/modules/frontend/deploy.tf). No framework, no
// dev server -- this is a build-once-then-sync-to-S3 static bundle.
import * as esbuild from "esbuild";
import { copyFileSync, mkdirSync, existsSync } from "fs";
import { fileURLToPath } from "url";
import path from "path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const srcDir = path.join(__dirname, "src");
const outDir = path.join(__dirname, "..", "terraform", "modules", "frontend", "build", "spa-dist");

mkdirSync(outDir, { recursive: true });

await esbuild.build({
  entryPoints: [path.join(srcDir, "app.js")],
  bundle: true,
  minify: true,
  sourcemap: false,
  target: ["es2020"],
  outfile: path.join(outDir, "app.js"),
  format: "esm",
  // The AWS SDK v3 packages (even the browser-safe ones) contain some
  // code paths that reference Node's `global` object. esbuild's browser
  // target does NOT polyfill this, so without the define below the page
  // fails at load with "global is not defined". `globalThis` is the standard, spec-defined browser
  // equivalent -- defining the identifier `global` as an alias for it at
  // build time (via esbuild's `define`) resolves every such reference
  // without needing a full Node polyfill package.
  define: {
    global: "globalThis",
  },
});

copyFileSync(path.join(srcDir, "index.html"), path.join(outDir, "index.html"));
copyFileSync(path.join(srcDir, "styles.css"), path.join(outDir, "styles.css"));

// runtime-config.json is written by Terraform's local_file resource
// BEFORE this build script runs (see deploy.tf) -- copy it into the dist
// output alongside the rest of the bundle so the SPA can fetch it at
// runtime, exactly as it fetches index.html/styles.css.
const runtimeConfigPath = path.join(srcDir, "runtime-config.json");
if (existsSync(runtimeConfigPath)) {
  copyFileSync(runtimeConfigPath, path.join(outDir, "runtime-config.json"));
} else {
  console.warn(
    "WARNING: runtime-config.json not found in src/ -- the SPA will fail to load its Cognito/AgentCore config. This file is written by Terraform's local_file resource; run `terraform apply` (or at least a `terraform plan` targeting that resource) before building standalone."
  );
}

console.log(`Build complete: ${outDir}`);
