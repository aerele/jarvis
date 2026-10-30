"""Holiday touches: a festival or birthday greeting on the Jarvis welcome screen.

What it does. ``resolve.answer(user)`` returns a small JSON payload (``kind`` is ``day``,
``leadup``, ``birthday`` or ``none``, plus the design to show) for the session user. It is
built from the user's Active Employee (date of birth, company) and the Holiday List HRMS
resolves for that employee: a Holiday row whose description names a festival (``match``),
or a weekly-off row that the country's own calendar names (``weekly_off``), within three days
either side of today (``pick``). The payload rides the desktop page boot
(``www/jarvis.py``) and ``api.get_today``. A date of birth is compared, never stored,
returned or logged.

Operator controls (all read on every request, so a change applies at once):

* Jarvis Settings ``holiday_touches_enabled``: the site switch for everything.
* Jarvis Settings ``birthday_greetings_enabled``: birthdays only.
* Jarvis User Settings ``show_holiday_touches``: one user's own switch.
* Jarvis Settings, section "Holiday policy (from Jarvis Cloud)": a read-only mirror of the
  control plane's switches (everything off, or named designs off), written only by
  ``policy.persist``. A site with a custom logo or agent name is white label and gets the
  words only.
* Site config ``jarvis_holiday_touch_force`` (``"diwali:day"``, ``"diwali:leadup"``,
  ``"birthday"``): a made-up occasion for testing; honoured only when ``developer_mode`` is on.

Support recipe, when a user says "I did not get a greeting" or "I got one by mistake":

1. ``bench --site <site> console``, then::

       from jarvis.holiday_touch import resolve
       resolve.occasion("<user>")

   The ``reason`` says why there is nothing (``no_access``, ``no_employee``,
   ``placeholder_dob``, ``no_hrms``, ``no_list``, ``no_rows``, ``no_match``, ``tie``,
   ``lookback``, ``holiday_error``) or how a hit was found. Pass ``now=datetime(2026, 11, 8,
   6, 0)`` (UTC) to preview another date. ``resolve.answer("<user>")`` is the payload the
   client gets, after the switches below.
2. Check the three switches and the policy mirror above.
   To see what the browser itself holds (the answer the page booted with, the one it shows
   after its own day check, the personal switch, the last fetch error and time), open the
   desktop app's browser console and read ``window.__holidayTouch``. It is read only.
3. If the data was just fixed but the answer has not changed, drop the cached answers::

       from jarvis.holiday_touch import cache
       cache.rotate()

   A change to an Employee, Company or Holiday List does this by itself once it commits.

Errors are logged by ``report`` as Error Log rows titled "Holiday touch: ...", at most one
per kind per hour. The rows are written deferred, so they appear in the Error Log only after
the scheduler flushes them (about every 15 minutes, and only while the scheduler is on).
"""
