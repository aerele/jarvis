"""Curated dashboard-canvas theme specs + the save-time HTML/CSS validator.

Each theme is a directory under ``themes/<key>/`` holding ``theme.json`` (the
machine-enforceable tokens / palette / scales + the validator allow-lists) and
``design.md`` (the human recipe and the source for the skill's generation
cheatsheet). ``theme_spec`` loads the JSON; ``theme_validator`` is a pure,
frappe-free deterministic linter the ``Jarvis Dashboard`` controller runs on
every save so no off-theme canvas HTML is ever stored.
"""
