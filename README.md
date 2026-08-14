Screenshots for issue #227 / PR #228 — the Webhook subscriptions panel on the
settings page. Assets only, no code. Delete this branch once the PR is merged.

Rendered from `modules/src/settings-daemon.py` at PR #228's head (aa0c1a5) with
its `@@include` markers expanded, driving the same local-webhook 0.13.0
`webhook.py` the module pins, in headless Chromium 151 at DPR 2.

| file | state directory | what it shows |
|---|---|---|
| `panel-live-state.png` | this box's real one | the panel as it is right now |
| `panel-all-row-states.png` | synthetic | all five row states plus two standing watches |
| `panel-narrow-420px.png` | synthetic | 420 px viewport — notes wrap, no horizontal scroll |
| `settings-page-in-context.png` | synthetic | where the panel sits between Sessions and Environment secrets |

`muted` and `broken filter` need a hand-written filter file, which is also the
only way those states arise: no tool writes `enabled:false` yet
(defangdevs/local-channels#23), and an unparseable filter is a botched edit.
The secrets shown are placeholder names in a throwaway env file.
