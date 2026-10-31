"""Fail closed before collaboration fixtures touch a site."""

import os


def require_collaboration_test_site(site):
	if site == "test_jarvis":
		return
	if site == "jarvis-ci.test" and os.environ.get("GITHUB_ACTIONS") == "true":
		return
	raise AssertionError("Collaboration tests require test_jarvis or the dedicated GitHub Actions site.")
