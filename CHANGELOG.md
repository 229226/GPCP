# Changelog

## 0.4.0 — 2026-11-14

- docs: `CONTRACT.md` (bundle / metrics / evaluation schemas, boundary rules,
  versioning) and `INTEGRATION.md` (how a new backend attaches, checklist,
  common mistakes).
- examples: `reference_backend.py`, a 60-line demo backend proving a third
  architecture attaches through the metrics contract only.
- scripts: `scripts/ci.sh` and a repo `Makefile` running build / verify /
  conformance / unit tests / sweep / demo in one command.
- conformance: new check that the demo backend attaches end to end.

## 0.3.0 — 2026-11-14

- P3: `numerology/{nr,lte,wifi}.py` plugin tables (v1 IR bytes unchanged),
  `cfg_schema`, sweep parameter context, `sweep` with STALE_GOLDEN detection.
- New `mmse_scalable` workload: parameterised N-layer x R-receiver MMSE whose
  channel, stimulus and golden all scale with `layers` / `rx`.

## 0.2.0 — 2026-11-14

- P2: metrics schema validation (rejects architecture-leaking keys),
  continuous-arrival on-time rate aligned with `load.simulate()`, and a
  conformance suite.

## 0.1.0 — 2026-11-14

- P1: standalone pip-installable package. 17 scenarios, 17 reference models,
  29 vendored pure-Python algorithm modules, `build` / `verify`.
- P0: frozen regression lock (52 artifacts + 131-row ledger).
