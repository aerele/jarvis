# Contributing

Jarvis is not accepting external code contributions or pull requests at this
time.

If you find a problem while using Jarvis, please report it through **Support**
inside the app.

## Branches and releases

| Branch | Role | What may merge into it |
|---|---|---|
| `develop` | default; all work lands here first | feature and fix PRs |
| `version-16-hotfix`, `version-15-hotfix` | backports waiting for the next release | backport PRs (cherry-picks from `develop`), fixes found in production |
| `version-16`, `version-15` | stable; what customers install and what Frappe Cloud tracks | **only** the release PR from the matching hotfix branch |

- A backport PR targets `version-N-hotfix`, never `version-N`. The `release-source` CI job
  fails any other PR into a stable branch.
- A fix found in production goes to the hotfix branch first, then is forward-ported to
  `develop` in its own PR so it is not lost on the next backport.
- A release is one PR, `version-N-hotfix` -> `version-N`, titled `chore: release vN.x.y`,
  merged with a merge commit. Before opening it, bump `__version__` in `jarvis/__init__.py`
  on the hotfix branch (feature backports bump minor, fix-only bumps patch). After the
  merge, push an annotated tag `vN.x.y` on the merge commit and publish a GitHub Release
  with generated notes from the previous tag.
- Nothing is pushed directly to any of these five branches.

