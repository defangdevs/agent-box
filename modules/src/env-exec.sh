# The per-user secrets file the settings page and `agent-box-session env`
# manage. Runs inside the user's session, so $HOME names it directly.
FILE="$HOME/.config/agent-box/env"
@@include:env-file.sh@@
# Exported literally — never eval'd, never re-split — so a secret full of
# shell metacharacters, or a multi-line PEM, arrives byte-exact.
env_export() { export "$1=$2"; }
env_parse "$FILE" env_export
exec "$@"
