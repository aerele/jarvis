"""R-21 guard — re-assert the protocol facts the fake gateway reproduces
against an explicitly selected runtime image, read-only, in a THROWAWAY
container (never the running pool).

Methodology matches spikes S1/S2 (which established the ordering + payload facts
at file:line against the vendored source and asserted SYMBOL PRESENCE in the
2026.6.8 image): here we re-confirm, in the operator-selected image, that
the tokens the harness depends on are present in /app/dist —

  * ack semantics       chat.send handler + idempotencyKey (ack runId echoes it)
                        + the lane-admission primitive enqueueCommandInLane
                        (its presence + S2's line-level ordering is the
                        "ack BEFORE lane admission" authority)
  * sessions.list flag  hasActiveRun (accept-inclusive per-session flag)
  * lane cap            maxConcurrent (main-lane default 4)
  * terminal metadata   stopReason + timeoutPhase + aborted (the lifecycle
                        end/error discriminators S1 relied on)
  * chat terminal state final / aborted / error broadcast states

Run: JARVIS-agnostic; needs docker + the local image. Read-only:
``docker run --rm --network none --entrypoint /bin/sh`` greps and exits; the
container is discarded. The running pool containers are never touched.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

# token -> what it evidences
REQUIRED = {
	"chat.send": "chat.send handler present",
	"idempotencyKey": "ack runId echoes caller idempotencyKey (S2 a)",
	"enqueueCommandInLane": "lane-admission primitive (post-ack; S2 a ordering)",
	"hasActiveRun": "sessions.list accept-inclusive active-run flag (S2 b)",
	"maxConcurrent": "main-lane concurrency cap, default 4 (S2 d)",
	"stopReason": "lifecycle terminal metadata key (S1)",
	"timeoutPhase": "turn-timeout terminal metadata key (S1)",
	"aborted": "user-abort terminal discriminator (S1)",
}


def _grep_count(token: str, image: str) -> tuple[int, str]:
	"""Return (file_count, first_file) for token across /app/dist, via a
	throwaway container. Non-zero file_count == present."""
	# -rl lists files containing the token; -F fixed-string (dots are literal).
	cmd = [
		"docker",
		"run",
		"--rm",
		"--network",
		"none",
		"--entrypoint",
		"/bin/sh",
		image,
		"-c",
		f"grep -rlF -- '{token}' /app/dist 2>/dev/null | head -50",
	]
	try:
		out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
	except subprocess.TimeoutExpired:
		return (-1, "TIMEOUT")
	files = [ln for ln in out.stdout.splitlines() if ln.strip()]
	first = files[0] if files else ""
	return (len(files), first)


def run(image: str) -> dict:
	# image present?
	have = subprocess.run(["docker", "images", "-q", image], capture_output=True, text=True).stdout.strip()
	if not have:
		return {"ok": False, "error": f"image {image} not present locally; cannot run R-21 probe"}

	results = {}
	all_pass = True
	for token, why in REQUIRED.items():
		count, first = _grep_count(token, image)
		present = count > 0
		all_pass = all_pass and present
		results[token] = {"present": present, "files": count, "example": first, "evidence": why}

	return {
		"ok": all_pass,
		"image": image,
		"method": "throwaway `docker run --rm --network none`; read-only grep of /app/dist; running pool untouched",
		"note": (
			"S2 line-verified the ack-BEFORE-lane-enqueue ordering (chat-DFeIryVW.js respond(true,ackPayload) "
			"at ~:2520 precedes embedded-agent enqueueCommandInLane) against 2026.6.11 source; this probe "
			"checks symbol presence in the selected image, matching the "
			"S1/S2 presence-assertion methodology. A behavioral ordering probe against the live stack was "
			"deliberately NOT run (R-21 is read-only)."
		),
		"results": results,
	}


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--image", required=True, help="Exact locally available runtime image to inspect")
	args = parser.parse_args()
	r = run(args.image)
	print(json.dumps(r, indent=2))
	sys.exit(0 if r.get("ok") else 1)


if __name__ == "__main__":
	main()
