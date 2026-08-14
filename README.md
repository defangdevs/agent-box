Screenshots for issue #227 / PR #228 — webhook subscriptions on the settings
page. Assets only, no code. Delete this branch once the PR is merged.

Rendered from `modules/src/settings-daemon.py` at PR #228's head (8532372) with
its `@@include` markers expanded, driving the same local-webhook 0.13.0
`webhook.py` the module pins, in headless Chromium 151 at DPR 2.

Since the review, a session's own subscriptions live in a fold under that
session's row in **Sessions**; the standing watches, which belong to no
session, keep a panel of their own.

| file | state directory | what it shows |
|---|---|---|
| `panel-live-state-v3.png` | this box's real one | the folds open on the box as it is right now |
| `panel-all-row-states-v3.png` | synthetic | every fold open: all five row states plus two standing watches |
| `panel-narrow-420px-v3.png` | synthetic | 420 px viewport — the chip wraps, no horizontal scroll |
| `settings-page-in-context-v3.png` | synthetic | the same page at rest: folded rows, each with its chip |

`muted` and `broken filter` need a hand-written filter file, which is also the
only way those states arise: no tool writes `enabled:false` yet
(defangdevs/local-channels#23), and an unparseable filter is a botched edit.
The secrets shown are placeholder names in a throwaway env file.
