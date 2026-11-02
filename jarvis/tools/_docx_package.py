"""Read selected OOXML members in memory. Never extract, fetch or evaluate content."""

import io
import posixpath
from collections import defaultdict
from urllib.parse import urlsplit
from xml.etree.ElementTree import TreeBuilder
from xml.parsers import expat
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile

from jarvis.exceptions import InvalidArgumentError

MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 2048
MAX_PARTS = 64
MAX_DEPTH = 256
MAX_NODES = 1_000_000
W_NAMESPACES = {
	"http://schemas.openxmlformats.org/wordprocessingml/2006/main",
	"http://purl.oclc.org/ooxml/wordprocessingml/main",
}
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
KNOWN_CHOICE_NAMESPACES = W_NAMESPACES | {
	"http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
	"http://schemas.microsoft.com/office/word/2010/wordprocessingGroup",
	"http://schemas.microsoft.com/office/word/2010/wordml",
	"http://schemas.openxmlformats.org/drawingml/2006/main",
	"http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
	"http://schemas.openxmlformats.org/drawingml/2006/picture",
	"http://purl.oclc.org/ooxml/drawingml/main",
	"urn:schemas-microsoft-com:vml",
}
DOCX_ERROR = "This DOCX could not be read. Re-save it as an unlocked DOCX, PDF or UTF-8 text file."


class DocxReadError(InvalidArgumentError):
	"""Format/resource refusal after the existing File permission check."""


def local(element):
	return element.tag.rsplit("}", 1)[-1]


def is_word(element):
	return element.tag.startswith("{") and element.tag[1:].split("}", 1)[0] in W_NAMESPACES


def attr(element, key, default=None):
	if element is None:
		return default
	allowed = W_NAMESPACES | {
		"http://schemas.openxmlformats.org/officeDocument/2006/relationships",
		"http://purl.oclc.org/ooxml/officeDocument/relationships",
	}
	return next(
		(
			v
			for k, v in element.attrib.items()
			if k == key
			or (k.startswith("{") and k[1:].split("}", 1)[0] in allowed and k.rsplit("}", 1)[-1] == key)
		),
		default,
	)


def child(element, name):
	if element is None:
		return None
	return next((x for x in element if is_word(x) and local(x) == name), None)


def integer(value, default=0, maximum=1_000_000_000):
	if value is None:
		return default
	if not isinstance(value, str) or len(value) > 10 or not value.isascii() or not value.isdecimal():
		raise DocxReadError(DOCX_ERROR)
	value = int(value)
	if value > maximum:
		raise DocxReadError("DOCX structure exceeds the supported limits. Split or simplify the document.")
	return value


class Package:
	def __init__(self, content):
		if content.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
			raise DocxReadError(
				"This Word file is encrypted or uses legacy .doc format. Supply an unlocked DOCX."
			)
		if not content.startswith(b"PK\x03\x04"):
			raise DocxReadError(DOCX_ERROR)
		try:
			self.archive = ZipFile(io.BytesIO(content))
		except (BadZipFile, ValueError):
			raise DocxReadError(DOCX_ERROR) from None
		infos = self.archive.infolist()
		self.members = {x.filename for x in infos}
		self.cache = {}
		self.choices = {}
		if len(infos) > MAX_ENTRIES or len(self.members) != len(infos):
			raise DocxReadError("DOCX contains too many or duplicate archive entries. Re-save the document.")
		if sum(x.file_size for x in infos) > MAX_EXPANDED_BYTES:
			raise DocxReadError("Expanded DOCX exceeds 64 MiB. Split or simplify the document.")
		for info in infos:
			if info.flag_bits & 1:
				raise DocxReadError("DOCX is encrypted. Supply an unlocked copy.")
			if info.compress_type not in {ZIP_STORED, ZIP_DEFLATED}:
				raise DocxReadError("DOCX uses unsupported ZIP compression. Re-save the document.")
			name = info.filename.rstrip("/")
			if (
				not name
				or name == ".."
				or "\\" in name
				or "\0" in info.orig_filename
				or name.startswith("/")
				or posixpath.normpath(name) != name
				or name.startswith("../")
			):
				raise DocxReadError("DOCX contains an unsafe archive path. Re-save the document.")

	def close(self):
		self.archive.close()

	def xml(self, name):
		if name in self.cache:
			return self.cache[name]
		if name not in self.members or len(name) > 256:
			raise DocxReadError("DOCX is missing a required text part. Re-save the document.")
		builder = TreeBuilder()
		parser = expat.ParserCreate(namespace_separator="}")
		depth = nodes = 0
		namespaces = defaultdict(list)

		def qname(value):
			return "{" + value if "}" in value else value

		def start(name, attrs):
			nonlocal depth, nodes
			depth += 1
			nodes += 1
			if depth > MAX_DEPTH or nodes > MAX_NODES:
				raise DocxReadError("DOCX XML structure exceeds the supported limits. Simplify the document.")
			element = builder.start(qname(name), {qname(k): v for k, v in attrs.items()})
			if element.tag == f"{{{MC}}}Choice":
				required = attrs.get("Requires", "").split()
				self.choices[id(element)] = bool(required) and all(
					namespaces[prefix] and namespaces[prefix][-1] in KNOWN_CHOICE_NAMESPACES
					for prefix in required
				)

		def end(name):
			nonlocal depth
			builder.end(qname(name))
			depth -= 1

		def denied(*_args):
			raise DocxReadError("DOCX contains a prohibited XML declaration. Re-save the document.")

		parser.StartElementHandler = start
		parser.EndElementHandler = end
		parser.CharacterDataHandler = builder.data
		parser.StartNamespaceDeclHandler = lambda prefix, uri: namespaces[prefix or ""].append(uri)
		parser.EndNamespaceDeclHandler = lambda prefix: namespaces[prefix or ""].pop()
		parser.StartDoctypeDeclHandler = denied
		parser.EntityDeclHandler = denied
		parser.ExternalEntityRefHandler = denied
		parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
		with self.archive.open(name) as stream:
			while data := stream.read(65536):
				parser.Parse(data, False)
		parser.Parse(b"", True)
		root = builder.close()
		self.cache[name] = root
		return root

	def target(self, base, target):
		if (
			not target
			or len(target) > 256
			or urlsplit(target).scheme
			or "\\" in target
			or "#" in target
			or "?" in target
		):
			raise DocxReadError("DOCX has an unsupported internal part reference. Re-save the document.")
		name = posixpath.normpath(
			target.lstrip("/") if target.startswith("/") else posixpath.join(posixpath.dirname(base), target)
		)
		if not name.startswith("word/") or name not in self.members:
			raise DocxReadError("DOCX refers to a missing or unsafe text part. Re-save the document.")
		return name

	def relationships(self, base=""):
		name = (
			posixpath.join(posixpath.dirname(base), "_rels", posixpath.basename(base) + ".rels")
			if base
			else "_rels/.rels"
		)
		if name not in self.members:
			return {}
		result = {}
		for element in self.xml(name):
			if local(element) != "Relationship":
				continue
			identifier = element.get("Id")
			if not identifier or identifier in result:
				raise DocxReadError(DOCX_ERROR)
			result[identifier] = (
				element.get("Type", "").rsplit("/", 1)[-1],
				element.get("Target", ""),
				element.get("TargetMode", "").lower() == "external",
			)
		return result

	def main_part(self):
		matches = [
			target
			for kind, target, external in self.relationships().values()
			if kind == "officeDocument" and not external
		]
		if len(matches) != 1:
			raise DocxReadError(DOCX_ERROR)
		main = self.target("", matches[0])
		types = self.xml("[Content_Types].xml")
		ctype = next((x.get("ContentType") for x in types if x.get("PartName") == "/" + main), "")
		if ctype != "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml":
			raise DocxReadError("This is not a DOCX text document. Use an unlocked, macro-free DOCX copy.")
		return main
