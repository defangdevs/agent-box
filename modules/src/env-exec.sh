# The per-user secrets file the settings page and `agent-box-session env`
# manage. Runs inside the user's session, so $HOME names it directly.
FILE="$HOME/.config/agent-box/env"
# One pair per NUL-separated record, because a value may span lines: a PEM
# certificate or private key (issue 212). $AGENT_BOX_ENV_CLI parses the file
# with systemd's own env-file grammar — the settings page writes it with the
# same code — and reports a problem on stderr; a session still starts, as it
# always did when the file was missing or unreadable.
if [ -r "$FILE" ]; then
  while IFS= read -r -d '' key && IFS= read -r -d '' val; do
    # Exported literally — never eval'd, never re-split — so a secret full
    # of shell metacharacters arrives byte-exact.
    export "$key=$val"
  done < <("$AGENT_BOX_ENV_CLI" read "$FILE")
fi
exec "$@"
