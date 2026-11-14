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
python3 -m wlbench.cli build --all
python3 -m wlbench.cli verify
python3 -m wlbench.cli conformance
python3 -m wlbench.cli evaluate \
  --bundle build/bundles/nr_mimo \
  --metrics examples/metrics_cgra.json
python3 -m wlbench.cli sweep --scenario mmse_scalable \
  --set layers=2,4,8 --set rx=2,4,8
python3 -m unittest discover -s tests -v
```

If `~/.local/bin` is on PATH, the `wlbench` console script can be used instead
of `python3 -m wlbench.cli`.

## Layout

```
src/wlbench/
  hll.py            frozen verb list + protocol parameter derivation
  numerology/       per-RAT protocol tables (nr / lte / wifi)
  cfg_schema.py     parameter whitelist and validation
  params.py         sweep parameter context
  scenarios/        17 frozen workloads + mmse_scalable (parameterised)
  models/           protocol-level reference semantics (stimulus + golden)
  tools/            vendored pure-Python algorithm closure (29 modules)
  build.py          ir.json / stimulus.bin / golden.json / manifest.json
  verify.py         byte-for-byte comparison with the P0 lock
  metrics.py        metrics contract + evaluate() timing verdict
  conformance.py    backend conformance suite
  sweep.py          parameter matrix + stale-golden detection
  cli.py            build / verify / evaluate / conformance / sweep
regression/
  lock.json         frozen sha256 of all 17 scenarios' artifacts
  baseline/         the exact bytes the current GPCP toolchain produces
  compare_all.csv   frozen 131-row cycle ledger
```

## Current status (P0 - P3)

- **P0** 17 scenarios, 131 ledger rows, 1.2 MB of baseline artifacts frozen.
- **P1** standalone pip-installable package; `build --all` reproduces all three
  artifacts byte-for-byte (`verify`: 52/52 OK).
- **P2** metrics contract validation, continuous-arrival on-time rate aligned
  with `load.profile()/load.simulate()`, and a 5-check conformance suite.
- **P3** numerology plugin tables, cfg schema, `params` context, `sweep` with
  stale-golden detection, and a genuinely parameterised `mmse_scalable`
  workload whose channel matrix, stimulus length and MMSE golden all scale
  with `layers` / `rx`.

The frozen 2x2 `nr_mimo` model is **not** parameterised (its channel and data
sizes are hard-coded). Sweeping MIMO size therefore uses `mmse_scalable`,
a separate workload. No existing scenario or reference model was modified.

- GPCP is untouched. It will consume these bundles via `backend/gpcp/` in P4.
- `mmse_scalable` is intentionally outside `regression/lock.json` (it is a new
  workload, not one of the 17 frozen ones); `verify` only checks the frozen set.

### Known carry-over (cleaned up later)

`src/wlbench/tools/` is a vendored compatibility layer: the original algorithm
modules are kept byte-identical (except for architecture words in comments) so
the regression lock holds. `models/` still carries historical unused
`REF_DUMP` / `SPEC` constants from the old evaluator; they do not affect
stimulus or golden generation. The scenario `import` line and the static
checker's whitelist were renamed from `proto.hll` to `wlbench.hll`; no
workload semantics changed.

## Next

P4 (GPCP adapter with full regression), P5 (CI and docs), P6 (protocol-process
extension: HARQ / multi-user / time-varying channel, new RAT tables).
