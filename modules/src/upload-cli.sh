# Put a local file into GitHub's attachment store and print the markdown that
# embeds it — the same store a human's drag-and-drop uses, so the result is a
# github.com/user-attachments/assets/<uuid> URL and NOT a file committed to the
# repo. Exists so a session can illustrate an issue or PR without committing
# binaries or parking them on a screenshot branch (issue #368).
#
# The endpoint is undocumented: no REST docs, no `gh` subcommand. That is
# precisely why it is wrapped here instead of being written out in the guide as
# a curl recipe — when it changes, one file changes, rather than every
# transcript that copied the recipe. Three details are load-bearing enough that
# they were shipped wrong in the guide first (PR #367 review):
#
#   1. The token goes in over stdin. curl does NOT blank -H in its arguments
#      and /proc carries no hidepid, so the obvious spelling publishes the
#      token to `ps` for every other Linux user on the box — and a Linux user
#      is the one boundary this system actually enforces (a session is not).
#      printf is a shell builtin, so nothing forks carrying the token either.
#   2. --fail-with-body. Plain `curl -sS` exits 0 on 4xx/5xx, which would hand
#      the caller GitHub's error JSON where a URL was expected.
#   3. The URL 404s until something references it, and goes live a few seconds
#      AFTER the comment posts. Anyone who verifies the URL before posting sees
#      a 404 on a perfectly good asset and concludes the upload failed, so this
#      says so on stderr rather than leaving it to be rediscovered.
set -u

PROG=agent-box-upload
# Overridable so the test can point at a local stand-in; there is no sandbox
# for the real endpoint and no test should depend on GitHub being reachable.
ENDPOINT=${AGENT_BOX_UPLOAD_ENDPOINT:-https://uploads.github.com/user-attachments/assets}

usage() {
  cat <<'EOF'
usage: agent-box-upload FILE [--repo OWNER/REPO] [--alt TEXT] [--url]

Upload FILE to GitHub's attachment store and print the markdown embed for it.
Images, GIFs, mp4 and PDFs all work — whatever a comment box accepts.

  --repo OWNER/REPO  repository the upload is charged against
                     (default: the repo of the current directory)
  --alt TEXT         alt text for the markdown (default: the file's name)
  --url              print the bare URL instead of a markdown embed
  -h, --help         this text

The printed URL 404s until an issue or PR body references it, and goes live a
few seconds after that comment posts. That is expected — do not check it and
conclude the upload failed. On a public repo the asset is public once live, so
never upload a terminal capture with a secret in it; against a private repo it
stays private, and GitHub serves it through a short-lived signed URL instead.

  agent-box-upload shot.png --repo defangdevs/agent-box
  gh issue comment 42 --body "$(agent-box-upload shot.png --alt before)"
EOF
}

die() { echo "$PROG: $*" >&2; exit 1; }

file=
repo=
alt=
want_url=0

while [ $# -gt 0 ]; do
  case $1 in
    -h|--help) usage; exit 0 ;;
    --repo) [ $# -ge 2 ] || die "--repo needs a value"; repo=$2; shift 2 ;;
    --alt)  [ $# -ge 2 ] || die "--alt needs a value";  alt=$2;  shift 2 ;;
    --url)  want_url=1; shift ;;
    --) shift; break ;;
    -*) die "unknown option $1 (see --help)" ;;
    *)  [ -z "$file" ] || die "only one FILE at a time (got $file and $1)"
        file=$1; shift ;;
  esac
done
[ -n "$file" ] || { usage >&2; exit 2; }
[ -f "$file" ] || die "no such file: $file"
[ -s "$file" ] || die "refusing to upload an empty file: $file"

command -v gh >/dev/null 2>&1 || die "gh is not on PATH"
command -v curl >/dev/null 2>&1 || die "curl is not on PATH"

name=${file##*/}
[ -n "$alt" ] || alt=$name

# The repo is not decoration: the upload is authorized against it, and it
# decides whether the asset ends up public or access-checked. So resolve it
# explicitly rather than defaulting to something surprising.
if [ -z "$repo" ]; then
  repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null) \
    || die "not in a GitHub repo — pass --repo OWNER/REPO"
  [ -n "$repo" ] || die "not in a GitHub repo — pass --repo OWNER/REPO"
fi

repo_id=$(gh api "repos/$repo" --jq .id 2>&1) || die "cannot read $repo: $repo_id"
case $repo_id in
  ''|*[!0-9]*) die "unexpected repository id for $repo: $repo_id" ;;
esac

# GitHub keys rendering off the declared content type, so a wrong one shows as
# a download link rather than an inline image. Trust `file` when it is present
# and fall back to the extension, which is what the browser upload does anyway.
ctype=
if command -v file >/dev/null 2>&1; then
  ctype=$(file --brief --mime-type "$file" 2>/dev/null) || ctype=
fi
if [ -z "$ctype" ]; then
  case $name in
    *.png) ctype=image/png ;;
    *.jpg|*.jpeg) ctype=image/jpeg ;;
    *.gif) ctype=image/gif ;;
    *.webp) ctype=image/webp ;;
    *.svg) ctype=image/svg+xml ;;
    *.mp4) ctype=video/mp4 ;;
    *.mov) ctype=video/quicktime ;;
    *.pdf) ctype=application/pdf ;;
    *) die "cannot tell the content type of $name — rename it with a real extension" ;;
  esac
fi

token=$(gh auth token 2>/dev/null) || token=
[ -n "$token" ] || die "no GitHub token — run 'gh auth login' or set GH_TOKEN"

# urlencode the query values: a file named "my shot.png" must not split the URL.
encode() {
  command -v jq >/dev/null 2>&1 || { printf '%s' "$1"; return; }
  printf '%s' "$1" | jq -sRr @uri
}

# The real endpoint carries no query of its own, but the test override may,
# and "?a=1?b=2" is not a URL anyone meant to build.
case $ENDPOINT in
  *\?*) sep='&' ;;
  *) sep='?' ;;
esac
url="$ENDPOINT$sep""name=$(encode "$name")&content_type=$(encode "$ctype")&repository_id=$repo_id"

# Header over stdin (see note 1 at the top). The body comes from a file, so
# stdin is free for exactly this.
response=$(
  printf 'Authorization: Bearer %s\n' "$token" |
    curl -sS --fail-with-body -X POST -H @- \
      -H "Content-Type: $ctype" \
      --data-binary "@$file" \
      "$url" 2>&1
) || die "upload failed: $response"

asset=$(printf '%s' "$response" | jq -r '.url // empty' 2>/dev/null) || asset=
[ -n "$asset" ] || die "upload returned no url: $response"

# The caveat goes to stderr so stdout stays a clean value to paste or pipe.
echo "$PROG: 404s until a comment references it, then goes live a few seconds after you post" >&2

if [ "$want_url" = 1 ]; then
  echo "$asset"
else
  echo "![$alt]($asset)"
fi
