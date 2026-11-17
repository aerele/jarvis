# source-contract-v1.json provenance

- Canonical repo: jarvis-fleet-agent
- Canonical path: `jarvis_fleet_agent/claude_redactor/contract/source-contract-v1.json`
- COMMIT: `cf272ae302cb9bcace38e5c483fd2f08f65f4b54` (canonical commit that last changed the copied bytes)
- Byte sha256: `bd12b22aaa684e8fc65e64543498f1d080989841e996d80ce7924cb8f8e30de8`
- Semantic digest (the 11 subset keys): `0e76718f09b9e549712ac7c0b814e73bb0631d7816c3d24be5e378b67fe3268c`

The copy is byte for byte. Never edit it to make a test pass. Change the canonical file, then re-sync and
re-pin here and in `jarvis/tests/test_source_receipt_contract.py`.

- Branding policy: this fixture holds the bridge prefix, which names the runtime. It has ONE exact line allowance in the runtime-branding policy (`jarvis/ci/data/branding-policy.b64`). Reason: cross-repo contract fixture, must be byte-identical to the plugin and fleet-agent copies.
