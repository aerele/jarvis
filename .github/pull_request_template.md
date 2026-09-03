## Summary

<!-- What does this change do, and why? -->

## Pre-merge checklist

> ℹ️ A red check only blocks the merge where the branch ruleset lists it as required.
> Honoring this checklist is what keeps broken changes out of UAT. See
> [`CONTRIBUTING.md`](../CONTRIBUTING.md).

- [ ] **CI is green** — the `tests` check on this PR passes (never merge on ❌)
- [ ] Branch is **up to date with its base** (`develop`, or `version-N-hotfix` for a backport)
- [ ] New/changed behavior has tests (the coverage gate still passes)
- [ ] I self-reviewed the diff
- [ ] If the base is `version-N`: this is the release PR from `version-N-hotfix`, `__version__` is bumped, and `release-source` is green
