#!/usr/bin/env bash
# Mirror the pinned claude-box module tarball to the S3 bucket so an IPv6-only
# box can fetch it over a dual-stack (IPv6-reachable) endpoint. github.com is
# IPv4-only, and on AWS the only NAT64 path to IPv4 is a NAT Gateway (which we
# deliberately avoid) - so the box fetches the module from S3 dual-stack instead.
#
# Idempotent; safe to run from both publish-template.yml and deploy-test.yml.
# Owns the bucket's public-read policy (template.yaml + module-*.tar.gz) so the
# two workflows never race on differing policy JSON. Requires: BUCKET env + AWS
# creds. The uploaded archive is byte-identical to github's, so the module's
# Nix NAR sha256 (ClaudeBoxSha256 in the template) is unchanged.
set -euo pipefail

: "${BUCKET:?BUCKET env required}"
TEMPLATE="${TEMPLATE:-aws/template.yaml}"

# Pinned rev = the ClaudeBoxRev parameter default in the template (single source
# of truth). Anchored to the parameter block so we don't match the hint URL in
# the ClaudeBoxSha256 description.
REV=$(awk '/^  ClaudeBoxRev:/{f=1} f&&/^ *Default:/{print $2; exit}' "$TEMPLATE")
if [ -z "${REV:-}" ]; then
  echo "::error::Could not parse ClaudeBoxRev default from $TEMPLATE" >&2
  exit 1
fi
KEY="module-${REV}.tar.gz"

echo "Ensuring bucket policy allows anonymous read of template.yaml + module-*.tar.gz..."
aws s3api put-bucket-policy --bucket "$BUCKET" --policy "$(cat <<POLICY
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "PublicReadClaudeBoxAssets",
    "Effect": "Allow",
    "Principal": "*",
    "Action": "s3:GetObject",
    "Resource": [
      "arn:aws:s3:::${BUCKET}/template.yaml",
      "arn:aws:s3:::${BUCKET}/module-*.tar.gz"
    ]
  }]
}
POLICY
)"

# Module tarballs are immutable per rev - skip the download+upload if present.
if aws s3api head-object --bucket "$BUCKET" --key "$KEY" >/dev/null 2>&1; then
  echo "s3://${BUCKET}/${KEY} already present; skipping upload."
  exit 0
fi

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
url="https://github.com/defangdevs/claude-box/archive/${REV}.tar.gz"
echo "Downloading ${url} ..."
curl -fsSL "$url" -o "$tmp"

echo "Uploading to s3://${BUCKET}/${KEY} ..."
aws s3 cp "$tmp" "s3://${BUCKET}/${KEY}" \
  --content-type "application/gzip" \
  --cache-control "public, max-age=31536000, immutable"
echo "::notice::Mirrored module ${REV} -> s3://${BUCKET}/${KEY}"
