# wlbench

Hardware-agnostic wireless protocol benchmark workloads.

The package describes **what** a protocol workload does and **what deadline** it
must meet. It never runs on hardware and never describes an architecture.
A hardware project consumes a workload bundle, runs it on its own backend, and
reports three generic quantities: `clock_hz`, `cycles`, `correct`. wlbench then
decides whether the protocol timing requirement was met.

## Boundary

| In wlbench | Reported by a backend | Forbidden here |
|---|---|---|
| `slot_ms` / deadlines / traffic model / on-time rate / gap table / `time = cycles ÷ clock_hz` / timing verdict | `clock_hz`, `cycles` (per stage + total), `correct` | `spad`, `bank`, `PE`, `IMEM`, `array_runs`, `DMA`, `microcode`, `tiling`, result-buffer offsets |

**Time budgets are in milliseconds and live in the bundle.** Cycle budgets are
derived as `slot_ms × clock_hz / 1000` at evaluation time. Never store a cycle
budget in a workload - that would silently freeze one clock frequency into the
protocol specification.

## Quick start

```bash
pip install -e .
python3 -m wlbench.cli build --all                      # -> build/bundles/<scenario>/
python3 -m wlbench.cli verify                           # check against regression/lock.json
python3 -m wlbench.cli evaluate \
  --bundle build/bundles/nr_mimo \
  --metrics examples/metrics_cgra.json   # timing verdict
python3 -m unittest discover -s tests -v
```

## Layout

```
src/wlbench/
  hll.py          frozen verb list + protocol parameter derivation
  scenarios/      17 workload descriptions (LTE / NR / WiFi)
  models/         protocol-level reference semantics (stimulus + golden)
  tools/          vendored pure-Python algorithm closure (29 modules)
  build.py        ir.json / stimulus.bin / golden.json / manifest.json
  verify.py       byte-for-byte comparison with the P0 lock
  metrics.py      metrics contract + evaluate() timing verdict
  cli.py          build / verify / evaluate
regression/
  lock.json       frozen sha256 of all 17 scenarios' artifacts
  baseline/       the exact bytes the current GPCP toolchain produces
  compare_all.csv frozen 131-row cycle ledger
```

## Current status (P0 + P1)

- P0 done: 17 scenarios, 131 ledger rows, 1.2 MB of baseline artifacts frozen.
- P1 done: standalone package, `wlbench build --all` reproduces `ir.json`,
  `stimulus.bin` and `golden.json` byte-for-byte (verified by `wlbench verify`).
- Timing contract implemented in `metrics.py` (single-arrival verdict, plus the
  metrics schema). Continuous-arrival on-time rate is aligned with `load.py` in P2.
- GPCP is untouched. It will consume these bundles via `backend/gpcp/` in P4.

### Known carry-over (cleaned up in P3)

`src/wlbench/tools/` is a vendored compatibility layer: the original algorithm
modules are kept byte-identical (except for architecture words in comments) so
the regression lock holds. Some `models/` files still carry historical
`REF_DUMP` / `platform` / `ref_stats` constants used by the old evaluator; they
do not affect stimulus or golden generation and will be removed when the
reference layer is rewritten. The scenario `import` line and the static
checker's whitelist were renamed from `proto.hll` to `wlbench.hll`; no workload
semantics changed.

## Next

P2 (conformance + sweep), P3 (cfg schema, numerology plugins, MIMO sweeps),
P4 (GPCP adapter with full regression), P5 (CI and docs).

If ~/.local/bin is on PATH, the `wlbench` console script can be used instead of `python3 -m wlbench.cli`.
