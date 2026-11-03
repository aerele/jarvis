"""Shared source-child environment, host capacity and value-free diagnostics."""

import fcntl
import json
import os
import signal
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

from jarvis.exceptions import InvalidArgumentError

MAX_CHILDREN = 4
BUSY = "File readers are busy. Retry after the current reads finish."
FAILED = (
	"The file reader failed before returning a result. Contact the operator; no complete result was returned."
)


def worker_env():
	# No bench credentials, tokens or site configuration enter a parser process.
	env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ") if key in os.environ}
	env.update(
		PYTHONPATH=str(Path(__file__).resolve().parents[2]),
		PYTHONDONTWRITEBYTECODE="1",
		OPENBLAS_NUM_THREADS="1",
		OMP_NUM_THREADS="1",
		MKL_NUM_THREADS="1",
		NUMEXPR_NUM_THREADS="1",
	)
	return env


def _slot_directory():
	return Path(tempfile.gettempdir()) / f"jarvis-source-workers-{os.getuid()}"


def _claim_slot():
	"""Four children per host uid, across sites and parent processes.

	Only empty lock files are stored. Pass the locked fd to the child so a parent
	crash cannot release its slot while that child still runs. The OS releases the
	lock when both processes close the fd, including on exit or forced termination.
	"""
	directory_fd = None
	try:
		directory = _slot_directory()
		directory.mkdir(mode=0o700, exist_ok=True)
		directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
		info = os.fstat(directory_fd)
		if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
			raise OSError("Unsafe capacity directory")
		for index in range(MAX_CHILDREN):
			fd = os.open(f"slot-{index}", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
			claimed = False
			try:
				info = os.fstat(fd)
				if (
					not stat.S_ISREG(info.st_mode)
					or info.st_uid != os.getuid()
					or stat.S_IMODE(info.st_mode) != 0o600
					or info.st_nlink != 1
				):
					raise OSError("Unsafe capacity file")
				try:
					fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
				except BlockingIOError:
					continue
				claimed = True
				return fd
			finally:
				if not claimed:
					os.close(fd)
	except OSError:
		raise InvalidArgumentError("File reader capacity is unavailable. Contact the operator.") from None
	finally:
		if directory_fd is not None:
			os.close(directory_fd)
	raise InvalidArgumentError(BUSY)


@contextmanager
def worker_slot():
	fd = _claim_slot()
	try:
		yield fd
	finally:
		os.close(fd)


def _diagnostic(kind, reason, status):
	# Stderr may contain source values. Log only fixed reason codes and exit status.
	try:
		import frappe

		frappe.logger("source-readers").warning("worker=%s reason=%s exit=%s", kind, reason, status)
	except Exception:
		pass


def check_exit(completed, kind, resource_error):
	if not completed.returncode:
		return
	stderr = getattr(completed, "stderr", "") or ""
	if completed.returncode in (-signal.SIGKILL, -signal.SIGXCPU, -signal.SIGALRM) or "MemoryError" in stderr:
		_diagnostic(kind, "resource_limit", completed.returncode)
		raise InvalidArgumentError(resource_error)
	reason = "import_failure" if "ImportError" in stderr or "ModuleNotFoundError" in stderr else "worker_exit"
	_diagnostic(kind, reason, completed.returncode)
	raise InvalidArgumentError(FAILED) from None


def fail_worker(kind, reason):
	_diagnostic(kind, reason, None)
	raise InvalidArgumentError(FAILED) from None


def decode_result(completed, kind):
	try:
		if len(completed.stdout) > 128 * 1024:
			raise ValueError
		response = json.loads(completed.stdout)
		if not isinstance(response, dict):
			raise ValueError
		if isinstance(response.get("error"), str):
			raise InvalidArgumentError(response["error"])
		if not isinstance(response.get("result"), dict):
			raise ValueError
		return response["result"]
	except (ValueError, KeyError, TypeError):
		_diagnostic(kind, "invalid_response", 0)
		raise InvalidArgumentError(FAILED) from None
