#!/usr/bin/env python3
"""SNR sweep for the sample-level SSB receiver (tools/ssb_plan.py models it).

Two modes, same geometry, both starting from tools/ssb_plan.build_td(snr_db=...):

  --model   pure Python: noisy samples -> FFT -> PSS (3) -> SSS (336) ->
            DMRS/equalise/descramble -> polar SC.  Fast sanity baseline.
  --hw      the same noisy samples through the CGRA: the SoC model runs
            sw/soc/ssb_fft_bm.c on the dumped samples and the result words are
            decoded exactly like tools/check_ssb_fft.py does.

Reported per point: N_ID2 ok, N_ID1 ok, MIB bit errors (of 32) and, for --hw,
the simulated cycles.  This is the first *quantitative* PHY metric of the
project (the rest of the suite is bit-exactness at one operating point).

    tools/sweep_snr.py --model --snr -6 -4 -2 0 2 4 6
    tools/sweep_snr.py --hw --snr 0 2 4 6 --seed 1
"""
import argparse
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ssb_plan as sp


def model_point(snr, seed):
    td, grid, payload = sp.build_td(snr_db=snr, seed=seed)
    sc = sp.subcarriers()
    y = [[sp._fft(td[s])[sc[r]] for r in range(sp.NRE)] for s in range(sp.NSYM)]

    # PSS: three candidates on the symbol that carries it
    ref = [sp.nr_seq.pss_seq(c) for c in range(3)]
    cor = []
    for c in range(3):
        re = sum(y[1][sp.PSS_RE0 + t].real * ref[c][t] for t in range(sp.NPSS))
        im = sum(y[1][sp.PSS_RE0 + t].imag * ref[c][t] for t in range(sp.NPSS))
        cor.append(abs(re) + abs(im))
    nid2 = max(range(3), key=lambda c: cor[c])

    # SSS: all 336 hypotheses on the other symbol
    seqs = [sp.nr_seq.sss_seq(h, nid2) for h in range(336)]

    def sss_metric(h):
        re = sum(y[3][sp.PSS_RE0 + t].real * seqs[h][t] for t in range(sp.NPSS))
        im = sum(y[3][sp.PSS_RE0 + t].imag * seqs[h][t] for t in range(sp.NPSS))
        return (re * re + im * im) ** 0.5

    best = max(range(336), key=sss_metric)
    h, llr = sp.receiver(y, best, nid2)
    bits = sp.sc_decode(llr)
    info = sp.info_indices()
    dec = [bits[i] for i in info]
    want = sp.payload_bits()
    mib_err = sum(1 for k in range(32) if dec[k] != want[k])
    return nid2, best, mib_err


def hw_point(snr, seed, bin_path, root):
    td_path = os.path.join(root, "build/ssb/td_snr.bin")
    os.makedirs(os.path.dirname(td_path), exist_ok=True)
    sub = subprocess.run([sys.executable, os.path.join(root, "tools/ssb_plan.py"),
                          "dump", td_path, "--snr", str(snr), "--seed", str(seed)],
                         capture_output=True, text=True, env={k: v for k, v in os.environ.items()
                                                             if k != "LD_LIBRARY_PATH"})
    if sub.returncode != 0:
        print(sub.stdout, sub.stderr)
        raise SystemExit("dump failed")
    dump = tempfile.mktemp(suffix=".txt")
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    cmd = [bin_path,
           "--load", os.path.join(root, "build/sw/ssb_fft_bm.bin") + "@0x80000000",
           "--load", td_path + "@0x80400000",
           "--load", os.path.join(root, "build/fft/twiddle.bin") + "@0x80100000",
           "--results", "0x80010000", "--nwords", "992",
           "--dump", dump, "--max-cycles", "40000000"]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    vals = []
    try:
        for line in open(dump):
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "word" and parts[2].startswith("0x"):
                w = int(parts[2], 16)
                vals.append(w & 0xFFFFFFFF)
                vals.append((w >> 32) & 0xFFFFFFFF)
    except OSError:
        raise SystemExit(f"no dump from {bin_path}; tail:\n{r.stdout[-500:]}{r.stderr[-500:]}")
    os.unlink(dump)
    if len(vals) < 992:
        raise SystemExit(f"short dump ({len(vals)} words); expected the program to finish:\n"
                         f"{r.stdout[-400:]}")
    nid2, true2 = vals[6], vals[7]
    nid1, true1 = vals[942], vals[943]
    mib = vals[954:986]
    want = sp.payload_bits()
    mib_err = sum(1 for k in range(32) if mib[k] != want[k])
    return nid2 == true2, nid1 == true1, mib_err, vals[9]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", action="store_true")
    ap.add_argument("--hw", action="store_true")
    ap.add_argument("--snr", type=float, nargs="+", default=[-4, -2, 0, 2, 4, 6, 8])
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--seeds", type=int, default=1,
                    help="average over seeds 1..N (noise realisations)")
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    a = ap.parse_args()
    if not (a.model or a.hw):
        ap.error("choose --model and/or --hw")

    root = os.path.abspath(a.root)
    print(f"{'SNR dB':>7} | {'mode':>5} | {'NID2 ok':>7} | {'NID1 ok':>7} | {'MIB err':>7} | cycles")
    print("-" * 58)
    for snr in a.snr:
        seeds = range(a.seed, a.seed + a.seeds)
        if a.model:
            e2 = e1 = 0
            errs = []
            for sd in seeds:
                n2, n1, err = model_point(snr, sd)
                e2 += n2 != sp.TRUE_NID2
                e1 += n1 != sp.TRUE_NID1
                errs.append(err)
            avg = sum(errs) / len(errs)
            print(f"{snr:7.1f} | model | {len(errs) - e2:3d}/{len(errs):<2d} | "
                  f"{len(errs) - e1:3d}/{len(errs):<2d} | {avg:7.2f} | -")
        if a.hw:
            bin_path = os.path.join(root, "sim/soc/build/Vgpcp_soc")
            if not os.path.exists(bin_path):
                raise SystemExit(f"missing {bin_path}: run make -C sim/soc all")
            if not os.path.exists(os.path.join(root, "build/sw/ssb_fft_bm.bin")):
                raise SystemExit("missing build/sw/ssb_fft_bm.bin: make -C sw/soc")
            os.environ.pop("LD_LIBRARY_PATH", None)
            e2 = e1 = 0
            errs = []
            cyc = 0
            for sd in seeds:
                ok2, ok1, err, cyc = hw_point(snr, sd, bin_path, root)
                e2 += not ok2
                e1 += not ok1
                errs.append(err)
            avg = sum(errs) / len(errs)
            print(f"{snr:7.1f} |    hw | {len(errs) - e2:3d}/{len(errs):<2d} | "
                  f"{len(errs) - e1:3d}/{len(errs):<2d} | {avg:7.2f} | {cyc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
