# Try an unmerged change on a test box

`scripts/test-box-update.py` installs a PR, branch or tag on an existing native
agent-box deployment (Ubuntu or another distro using the Nix runtime profile).
It keeps the original runtime closure alive through Nix garbage collection and
records its exact revision for an explicit restore. It does not require merging
the helper or the candidate first.

This is an operator tool for a disposable test box. Run it from a root shell,
outside the agent service that the update restarts. Obtain a trusted copy of the
helper as root and keep it outside the runtime profile being replaced. Do not
execute an agent-writable checkout as root. Use your provider's operator console
or an SSH administrator account. The agent's existing sudo exception and the
settings page continue to update from upstream's default branch only.

From the directory containing that trusted copy:

```sh
# Resolve PR #923 without moving the tree, pinning a closure or installing.
python3 test-box-update.py install --pr 923 --check

# Save the baseline and install that PR's immutable head commit.
python3 test-box-update.py install --pr 923

# Or use a branch/tag instead, after restoring any previous trial.
python3 test-box-update.py install --ref refs/heads/feat/922-secret-custody

# Show the saved baseline, candidate and currently installed revision.
python3 test-box-update.py status

# Return to the exact revision installed before the trial.
python3 test-box-update.py restore
```

The branch/PR ref is resolved once and the update receives the full commit ID.
The helper deliberately allows divergence and downgrades through the existing
root-only `agentbox update --force` path. Normal updates retain their ancestry
check. Candidate code will run as root, so inspect it before choosing it.

The default profile is `/nix/var/nix/profiles/agent-box`; `install --config PATH`
selects a different native configuration file. `--profile` and `--state-dir`
are available on every subcommand and must be supplied consistently when
overridden. State defaults to `/var/lib/agent-box/test-update`, mode 0700.

Only one trial can be recorded at a time. Failed installs and failed restores
retain the baseline and recovery record. A successful restore removes the
record and GC root. If cleanup is interrupted, `status` reports a pending
cleanup marker and rerunning `restore` finishes cleanup without another update.
Status is read-only and can show the saved record even when the baseline CLI
is missing; the current revision is null when profile metadata is unreadable.
Failed writes before the trial record is published release the temporary pin.
Recovery uses the pinned original updater executable, even
if the candidate's helper interfaces change. Keep the standalone script until
restoration is complete. Do not run normal updates or edit the configuration
during a trial.

This restores software, not application data or configuration files changed by
the candidate. It does not implement automatic recovery after a broken boot or
loss of the front door, and restoring a revision may need network access for
the source tree. Retain the provider console or SSH recovery path. NixOS system
generations require a different mechanism; this helper refuses a box without
an identifiable native runtime profile.

If a PR predates configuration fields enabled on the test box, its old renderer
cannot apply the current configuration. Prepare an integration branch from the
box's installed revision plus that PR's changes, review the diff, and select it
with `--ref`. Record both commits in the test evidence: this exercises the PR's
changes while keeping the box's existing configuration supported; it does not
establish compatibility of the PR's original head with that configuration.

## Testing #922 / PR #923

PR #923 currently supplies custody payloads and nine unit tests, without native
renderer wiring, units, a settings UI, command jobs or MCP integration. Installing
it tests the runtime built from that branch but does not enable the complete
secrets feature in #922. Run `python3 tests/test-secret-custody.py` in its checkout
for the existing protocol/CLI tests. OS-boundary tests and the rest of the
implementation remain required before the complete feature can be exercised.
