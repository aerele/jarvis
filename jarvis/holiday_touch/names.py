"""The name tables behind holiday matching. Pure data, no logic.

Every phrase here is written in its NORMALISED form (lowercase, letters and digits only,
single spaces), the form ``match.normalise`` produces for row text. Admin-supplied aliases
and near-misses are normalised the same way before they are merged in.
"""

from __future__ import annotations

# The nine design keys. The design decides which art plays; "republic" is an occasion
# that reuses the "independence" art.
DESIGNS = (
	"birthday",
	"christmas",
	"diwali",
	"pongal",
	"eid",
	"newyear",
	"onam",
	"holi",
	"independence",
)

# occasion -> design. Birthday is a design but never a holiday-row occasion.
OCCASIONS = {
	"christmas": "christmas",
	"diwali": "diwali",
	"pongal": "pongal",
	"eid": "eid",
	"newyear": "newyear",
	"onam": "onam",
	"holi": "holi",
	"independence": "independence",
	"republic": "independence",
}

ALIASES = {
	"diwali": ("diwali", "deepavali", "deepawali", "divali"),
	"pongal": ("pongal", "thai pongal", "mattu pongal", "maattu pongal", "kaanum pongal"),
	"eid": (
		"eid",
		"eid ul fitr",
		"eid al fitr",
		"id ul fitr",
		"idul fitr",
		"ramzan",
		"ramadan",
		"bakrid",
		"bakri eid",
		"eid ul adha",
		"eid al adha",
		"id ul zuha",
	),
	"christmas": ("christmas", "christmas day", "xmas", "x mas"),
	"newyear": ("new year", "new years day", "new year s day", "newyear"),
	"onam": ("onam", "thiruvonam", "thiru onam"),
	"holi": ("holi", "dhulandi", "dhuleti", "dhulivandan"),
	"independence": ("independence day",),
	"republic": ("republic day",),
}

# Phrases that contain an alias but are a different day. Their spans are removed before
# alias matching, so "Christmas Eve" or "Parsi New Year" never reaches a design.
NEAR_MISSES = (
	"christmas eve",
	"tamil new year",
	"telugu new year",
	"kannada new year",
	"parsi new year",
	"chinese new year",
	"lunar new year",
	"islamic new year",
	"hijri new year",
	"new year eve",
	"new year s eve",
	"holika dahan",
	"bhogi",
	"choti diwali",
	"chhoti diwali",
	"day after diwali",
	"naraka chaturdashi",
	"meshadi",
	"milad",
	"milad un nabi",
	"eid e milad",
	"eid milad",
	"eid ul milad",
	"eid milad un nabi",
	"first day of ramadan",
	"ramadan begins",
)

# occasion -> (month, day, india_only). A failed guard means no match.
DATE_GUARDS = {
	"newyear": (1, 1, False),
	"christmas": (12, 25, False),
	"independence": (8, 15, True),
	"republic": (1, 26, True),
}
