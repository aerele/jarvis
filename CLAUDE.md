# Jarvis: rules for coding agents

Coding agents load this file by themselves at the start of a session. The rules also
apply to people. Branch and release rules are in [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Writing tests

A test earns its place when it catches a break that no other test catches. Before
you add one, name that break in one sentence. If you cannot, do not add the test.

Do not add a test that:

- has no assertion, or only checks "does not raise" on code that catches its own errors
- mocks the function that does the work, then asserts the value the mock returned
- compares a constant with a copy of itself, or with the formula that defines it
- repeats the input of an existing test and checks less than that test does
- only checks a state that the class `setUp` already needs for every other test

When you move a behaviour's tests to a new file, delete the old copies in the same
change. When one table-style test covers many rows, do not add a one-row twin.

Keep tests that guard permissions, ownership, money or budget limits, stored data
shapes, races, and defaults a user sees, even when they look like duplicates.
