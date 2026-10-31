"""Bound report IPC by bytes and wall time. Full source rows stay in the child."""

import base64
import json
import os
import select
import subprocess
import sys
import time

from jarvis.exceptions import InvalidArgumentError
from jarvis.tools._report_query_data import (
	MAX_ANSWER,
	MAX_COMPRESSED,
	MAX_CONTEXT,
	MAX_PROOFS,
	MAX_SCHEMA,
	encode,
)
from jarvis.tools._source_process import worker_env, worker_slot

WALL_SECONDS = 22
RESOURCE_ERROR = "Prepared report analysis exceeded the resource budget. Narrow the report. No complete answer was returned."


class Deadline:
	def __init__(self, seconds=WALL_SECONDS):
		self.end = time.monotonic() + seconds

	def remaining(self):
		left = self.end - time.monotonic()
		if left <= 0:
			raise InvalidArgumentError(RESOURCE_ERROR)
		return left

	def check(self):
		self.remaining()


class ReportWorker:
	def __init__(self, deadline):
		self.deadline = deadline
		self.process = None
		self.buffer = bytearray()
		self.received = 0
		self.capacity = None

	def __enter__(self):
		self.capacity = worker_slot()
		slot = self.capacity.__enter__()
		# Security review: fixed interpreter/module argv, shell disabled. Caller
		# data is framed JSON on stdin, never executable arguments.
		try:
			self.process = subprocess.Popen(  # nosemgrep: frappe-subprocess-exec
				[sys.executable, "-m", "jarvis.tools._report_query_worker"],
				stdin=subprocess.PIPE,
				stdout=subprocess.PIPE,
				stderr=subprocess.DEVNULL,
				shell=False,
				env=worker_env(),
				pass_fds=(slot,),
			)
			os.set_blocking(self.process.stdin.fileno(), False)
			os.set_blocking(self.process.stdout.fileno(), False)
		except BaseException:
			self.__exit__()
			raise
		return self

	def send(self, data, limit=MAX_CONTEXT):
		payload = encode(data) + b"\n"
		if len(payload) > limit:
			raise InvalidArgumentError("Report request exceeded its data budget. Narrow the report.")
		fd, offset = self.process.stdin.fileno(), 0
		try:
			while offset < len(payload):
				_, ready, _ = select.select([], [fd], [], self.deadline.remaining())
				if not ready:
					raise InvalidArgumentError(RESOURCE_ERROR)
				try:
					offset += os.write(fd, payload[offset : offset + 65536])
				except BlockingIOError:
					continue
		except (BrokenPipeError, OSError):
			raise InvalidArgumentError(RESOURCE_ERROR) from None

	def receive(self, limit):
		while b"\n" not in self.buffer:
			ready, _, _ = select.select([self.process.stdout.fileno()], [], [], self.deadline.remaining())
			if not ready:
				raise InvalidArgumentError(RESOURCE_ERROR)
			chunk = os.read(self.process.stdout.fileno(), min(65536, limit + 1 - len(self.buffer)))
			if not chunk:
				raise InvalidArgumentError(RESOURCE_ERROR)
			self.buffer.extend(chunk)
			self.received += len(chunk)
			if len(self.buffer) > limit or self.received > MAX_SCHEMA + MAX_PROOFS + MAX_ANSWER + 2048:
				raise InvalidArgumentError(
					"Report worker output exceeded its data budget. Narrow the report."
				)
		line, _, rest = self.buffer.partition(b"\n")
		self.buffer = bytearray(rest)
		try:
			frame = json.loads(line)
		except (ValueError, UnicodeError):
			raise InvalidArgumentError("Report worker returned an invalid response.") from None
		if not isinstance(frame, dict):
			raise InvalidArgumentError("Report worker returned an invalid response.")
		if "error" in frame:
			raise InvalidArgumentError(str(frame["error"])[:1000])
		return frame

	def __exit__(self, *_):
		try:
			if self.process is not None:
				for pipe in (self.process.stdin, self.process.stdout):
					if pipe is not None:
						pipe.close()
				if self.process.poll() is None:
					self.process.kill()
				self.process.wait()
		finally:
			if self.capacity is not None:
				self.capacity.__exit__(None, None, None)
				self.capacity = None


def analyze_report(content, metadata, *, authorize, validate, sql=None, max_rows=100, deadline=None):
	if len(content) > MAX_COMPRESSED:
		raise InvalidArgumentError("Prepared report exceeds the 10 MiB compressed input limit.")
	deadline = deadline or Deadline()
	with ReportWorker(deadline) as worker:
		worker.send({"content": base64.b64encode(content).decode("ascii"), **metadata}, 14 * 1024 * 1024)
		schema = worker.receive(MAX_SCHEMA).get("schema")
		context = authorize(schema, deadline)
		worker.send(context)
		proofs = worker.receive(MAX_PROOFS + 1024).get("proofs")
		validate(schema, context, proofs, deadline)
		deadline.check()
		worker.send({"sql": sql, "max_rows": max_rows})
		result = worker.receive(MAX_ANSWER).get("result")
		if not isinstance(result, dict) or result.get("coverage", {}).get("complete") is not True:
			raise InvalidArgumentError("Prepared report worker did not confirm a complete source read.")
		return result
