"""A bounded subset of Word list labels; unsupported labels are explicit gaps."""

from jarvis.tools._docx_package import DocxReadError, attr, child, integer, is_word, local

MAX_DEFINITIONS = 1024
FORMATS = {"decimal", "decimalZero", "lowerLetter", "upperLetter", "lowerRoman", "upperRoman", "bullet"}


def val(element, name, default=None):
	node = child(element, name)
	return attr(node, "val", default) if node is not None else default


class Numbering:
	def __init__(self, numbering, styles, issue):
		self.issue = issue
		self.abstract = {}
		self.nums = {}
		self.styles = {}
		self.counts = {}
		self.used_abstract = {}
		if styles is not None:
			for style in styles:
				if is_word(style) and local(style) == "style":
					if len(self.styles) >= MAX_DEFINITIONS:
						raise DocxReadError("DOCX contains too many styles. Simplify the document.")
					self.styles[attr(style, "styleId")] = (child(style, "pPr"), val(style, "basedOn"))
		if numbering is None:
			return
		for item in numbering:
			if not is_word(item):
				continue
			if local(item) == "abstractNum":
				levels = {}
				for level in item:
					if local(level) != "lvl":
						continue
					idx = integer(attr(level, "ilvl"), maximum=8)
					levels[idx] = self.level(level)
				self.abstract[attr(item, "abstractNumId")] = levels
			elif local(item) == "num":
				overrides = {}
				for item_override in item:
					if local(item_override) != "lvlOverride":
						continue
					idx = integer(attr(item_override, "ilvl"), maximum=8)
					overrides[idx] = (val(item_override, "startOverride"), child(item_override, "lvl"))
				self.nums[attr(item, "numId")] = (val(item, "abstractNumId"), overrides)
			if len(self.abstract) > MAX_DEFINITIONS or len(self.nums) > MAX_DEFINITIONS:
				raise DocxReadError("DOCX contains too many list definitions. Simplify the document.")

	def reset(self):
		self.counts = {}
		self.used_abstract = {}

	def level(self, node):
		return {
			"start": integer(val(node, "start"), 1),
			"fmt": val(node, "numFmt", "decimal"),
			"text": val(node, "lvlText", "%1."),
			"suffix": val(node, "suff", "tab"),
			"restart": val(node, "lvlRestart"),
			"legal": child(node, "isLgl") is not None and val(node, "isLgl") not in {"0", "false", "off"},
		}

	def num_props(self, paragraph):
		props = child(paragraph, "pPr")
		if props is None:
			return None
		direct = child(props, "numPr")
		if direct is not None:
			return direct
		style = self.styles.get(val(props, "pStyle"))
		for _ in range(2):
			if not style:
				return None
			style_props, parent = style
			if style_props is not None:
				found = child(style_props, "numPr")
				if found is not None:
					return found
			style = self.styles.get(parent)
		if style:
			self.issue("list_numbering_partial")
		return None

	def prefix(self, paragraph):
		props = self.num_props(paragraph)
		if props is None:
			return ""
		num_id = val(props, "numId")
		if num_id == "0":
			return ""
		level_idx = integer(val(props, "ilvl"), maximum=8)
		if num_id not in self.nums:
			self.issue("list_numbering_partial")
			return ""
		abstract, overrides = self.nums[num_id]
		used = self.used_abstract.setdefault(abstract, set())
		used.add(num_id)
		levels = dict(self.abstract.get(abstract, {}))
		for idx, (start, definition) in overrides.items():
			if definition is not None:
				levels[idx] = self.level(definition)
			if idx in levels and start is not None:
				levels[idx] = {**levels[idx], "start": integer(start)}
		level = levels.get(level_idx)
		if (
			not level
			or level["fmt"] not in FORMATS
			or len(level["text"]) > 256
			or level["restart"] not in {None, "0"}
		):
			self.issue("list_numbering_partial")
			return ""
		if len(used) > 1:
			referenced = {level_idx} | {idx for idx in range(9) if "%" + str(idx + 1) in level["text"]}
			if any(overrides.get(idx, (None, None))[0] is None for idx in referenced):
				# A child restart does not establish its parent clause references.
				self.issue("list_numbering_partial")
				return ""
		counts = self.counts.setdefault(num_id, {})
		counts[level_idx] = counts.get(level_idx, level["start"] - 1) + 1
		for idx in list(counts):
			if idx > level_idx and levels.get(idx, {}).get("restart") != "0":
				del counts[idx]
		try:
			if level["fmt"] == "bullet":
				return "•\t"
			label = level["text"]
			for idx in range(9):
				if "%" + str(idx + 1) not in label:
					continue
				definition = levels[idx]
				fmt = "decimal" if level["legal"] and definition["fmt"] != "bullet" else definition["fmt"]
				label = label.replace(
					"%" + str(idx + 1), self.render(counts.get(idx, definition["start"]), fmt)
				)
			if any("%" + str(idx) in label for idx in range(10)):
				raise ValueError
			return label + (
				" " if level["suffix"] == "space" else "" if level["suffix"] == "nothing" else "\t"
			)
		except (ValueError, KeyError):
			self.issue("list_numbering_partial")
			return ""

	@staticmethod
	def render(number, fmt):
		if not 0 <= number <= 1_000_000_000:
			raise ValueError
		if fmt == "decimal":
			return str(number)
		if fmt == "decimalZero":
			return f"{number:02}"
		if fmt in {"lowerLetter", "upperLetter"}:
			if number < 1:
				raise ValueError
			text = ""
			while number:
				number, digit = divmod(number - 1, 26)
				text = chr(65 + digit) + text
			return text.lower() if fmt == "lowerLetter" else text
		if fmt not in {"lowerRoman", "upperRoman"} or not 1 <= number <= 3999:
			raise ValueError
		text = ""
		for value, token in [
			(1000, "M"),
			(900, "CM"),
			(500, "D"),
			(400, "CD"),
			(100, "C"),
			(90, "XC"),
			(50, "L"),
			(40, "XL"),
			(10, "X"),
			(9, "IX"),
			(5, "V"),
			(4, "IV"),
			(1, "I"),
		]:
			while number >= value:
				text += token
				number -= value
		return text.lower() if fmt == "lowerRoman" else text
