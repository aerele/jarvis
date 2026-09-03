## Summary

<!-- What does this change do, and why? -->

## Pre-merge checklist

> ℹ️ These repos are private on the GitHub **Free** plan, so branch protection is
> **not enforced** — CI cannot hard-block a merge. Honoring this checklist is what keeps
> broken changes out of UAT. See [`CONTRIBUTING.md`](../CONTRIBUTING.md).

- [ ] **CI is green** — the `tests` check on this PR passes (never merge on ❌)
- [ ] Branch is **up to date with its base** (`develop`, or `version-N-hotfix` for a backport)
- [ ] New/changed behavior has tests (the coverage gate still passes)
- [ ] I self-reviewed the diff
