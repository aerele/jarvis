# test_account Triage Report

## Failures Found
10 pre-existing failures across 5 test classes:
- TestAdminChatGate (1-2 tests)
- TestAuthorityAnchorFence (1 test)
- TestReplacedSiteIsExplained (2-3 tests)
- TestIsReadyForChatCohorts (1 test)
- TestBackfillExcludesOauthPushOnly (1 test)

## Root Cause
**Site staleness.** All failures were `readiness_unconfirmed` with `diag_code=admin_error`, indicating the test_jarvis database schema was out of sync with the application code.

## Resolution
Running `bench --site test_jarvis migrate` clears all 10 failures. After migration, the full test_account suite runs 79/79 green.

## Verification
Before migrate: 79 tests run, 10 failures.
After migrate: 79 tests run, 0 failures.
