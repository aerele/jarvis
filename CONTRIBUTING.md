# Contributing

Jarvis is not accepting external code contributions or pull requests at this
time.

If you find a problem while using Jarvis, please report it through **Support**
inside the app.

## Branches and releases

Runtime branding and upgrade tests use checksum-pinned snapshots of Admin-owned
inputs, stored encoded under `jarvis/ci/data`. CI validates and decodes them into
temporary files outside the checkout; no repository variables or credentials are
needed. Review snapshot changes with their Admin source documents and checksum
pins. Do not generate migration fixtures from production constants.

For local validation, run `python -m jarvis.ci.inputs policy --output /tmp/jarvis-policy.json`
then `python -m jarvis.ci.runtime_branding --policy /tmp/jarvis-policy.json`.

| Branch | Role | What may merge into it |
|---|---|---|
| `develop` | default; all work lands here first | feature and fix PRs |
| `version-16-hotfix`, `version-15-hotfix` | backports waiting for the next release | backport PRs (cherry-picks from `develop`), fixes found in production |
| `version-16`, `version-15` | stable; what customers install and what Frappe Cloud tracks | **only** the release PR from the matching hotfix branch |

- A backport PR targets `version-N-hotfix`, never `version-N`. The `release-source` check
  (`.github/workflows/release-guard.yml`) fails any other PR into a stable branch. It
  blocks the merge only where the branch ruleset lists it as a required check; wire that
  in repo settings, the workflow cannot do it by itself.
- A fix found in production goes to the hotfix branch first, then is forward-ported to
  `develop` in its own PR so it is not lost on the next backport.
- A release is one PR, `version-N-hotfix` -> `version-N`, titled `chore: release vN.x.y`,
  merged with a merge commit. Before opening it, bump `__version__` in `jarvis/__init__.py`
  on the hotfix branch (feature backports bump minor, fix-only bumps patch). On merge the
  `Release` workflow (`.github/workflows/release.yml`) tags the merge commit `vN.x.y` and
  publishes a GitHub Release with notes generated from the previous tag on that line.
  Check the Releases page afterwards; if the run failed, re-run it from the Actions tab.
  It resumes whatever step was missing (tag, Release, or nothing).
- Never push directly to any of these five branches; everything lands through a PR.
  The `version-N` rulesets enforce this today, and the `version-N-hotfix` rulesets should
  match them (PR required, no force-push, no deletion).
- On a release PR the same check also fails unless `__version__` moved up and its major
  matches the line, so a release cannot ship without the bump.

## Upgrade regression tests

Legacy upgrade tests require the independently maintained Admin fixture. Set
`JARVIS_LEGACY_MIGRATION_FIXTURES_FILE` to an absolute path to
`integration_fixtures/legacy_migrations.json` in the Admin checkout before running
these tests on a dedicated test site. For the pinned local snapshot, run
`python -m jarvis.ci.inputs fixture --output /tmp/jarvis-migration-fixture.json`
and set the environment variable to that file. CI uses this same snapshot. Missing inputs fail the tests; keep decoded copies
outside this repository. See Admin's
`jarvis_admin_v2/docs/legacy-migration-test-fixtures.md` for activation and commands.
