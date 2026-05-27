# Gondolin protocol benchmarks

Throwaway scripts that measured the wire-protocol switch for the gondolin
terminal backend (JSON+base64 → length-prefixed msgpack). Numbers from
these runs are captured in `docs/design/gondolin-terminal-backend.md` and
in the commit message that flipped the wire.

Kept in-repo so the perf claims are reproducible. Likely to be pruned
once the backend stabilises.

## Files

- `protocol_compare.py` — Python side: encode + decode the same chunk
  corpora through both protocols, report wire size + time. Run with
  `python bench/protocol_compare.py`.
- `protocol_compare.mjs` — Node side equivalent. Run with
  `node bench/protocol_compare.mjs` from the repo root (needs
  `@msgpack/msgpack` available; easiest via
  `tools/environments/gondolin_host/node_modules`).
- `gondolin_protocol.py` — end-to-end bench that drives the real daemon
  over the chosen wire. Requires KVM + a built `gondolin_host/`.
