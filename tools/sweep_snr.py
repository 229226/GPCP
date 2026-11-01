#!/usr/bin/env python3
"""AWGN + SNR->BER sweep for the P1 real-mapping SSB PBCH chain.

Runs the Python golden reference receiver (tools/ssb_plan_p1.py) over a flat
channel + per-subcarrier AWGN at a grid of SNRs and reports the block-error /
bit-error rate of the decoded MIB.  This is the *model* curve: it exercises the
exact same 38.211/38.212 blocks the C benchmark mirrors, so it is the reference
the silicon result must sit on.

Optional: with --hw-dir, also dump one noisy time-domain sample file per SNR
(same generator the C benchmark loads) so the hardware can be anchored at the
model points via `make run-ssb-fid SNR=<db>`.

Usage:
    python3 tools/sweep_snr.py                       # print table + write csv
    python3 tools/sweep_snr.py --hw-dir build/soc/hw
    python3 tools/sweep_snr.py --snr-min -5 --snr-max 25 --snr-step 1 --trials 40
"""
import os
import sys
import argparse

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import ssb_plan_p1 as P   # noqa: E402


def ber_at(snr_db, trials):
    """Average MIB BER over `trials` noisy draws (each a fresh payload/seed)."""
    errs = 0
    tot = 0
    for i in range(trials):
        seed = (0x50B0A5 + i * 2654435761) & 0xFFFFFFFF
        rx, payload = P.run_one(snr_db=snr_db, seed=seed)
        errs += sum(1 for a, b in zip(rx, payload) if a != b)
        tot += len(payload)
    return errs, tot


def main():
    ap = argparse.ArgumentParser(description="P1 SSB PBCH AWGN SNR-BER sweep")
    ap.add_argument("--snr-min", type=float, default=-6.0)
    ap.add_argument("--snr-max", type=float, default=26.0)
    ap.add_argument("--snr-step", type=float, default=1.0)
    ap.add_argument("--trials", type=int, default=30)
    ap.add_argument("--out", default="build/soc/ssb_fid_snr.csv")
    ap.add_argument("--hw-dir", default=None,
                    help="if set, dump one noisy TD per SNR for HW anchoring")
    args = ap.parse_args()

    snrs = []
    s = args.snr_min
    while s <= args.snr_max + 1e-9:
        snrs.append(round(s, 3))
        s += args.snr_step

    print("# P1 SSB PBCH: AWGN SNR -> MIB BER (model, 38.211/38.212 chain)")
    print("# snr_db  ber        errors  bits")
    rows = []
    for snr in snrs:
        e, t = ber_at(snr, args.trials)
        ber = e / t if t else 0.0
        rows.append((snr, ber, e, t))
        print("%7.2f  %.4e  %7d  %7d" % (snr, ber, e, t))

    out = args.out
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as f:
        f.write("snr_db,ber,errors,bits\n")
        for snr, ber, e, t in rows:
            f.write("%.3f,%.6e,%d,%d\n" % (snr, ber, e, t))
    print("# wrote %s" % out)

    if args.hw_dir:
        os.makedirs(args.hw_dir, exist_ok=True)
        for snr in snrs:
            fn = os.path.join(args.hw_dir, "ssb_fid_%d.td" % int(round(snr)))
            P.dump_td_bin(fn, snr_db=snr, lcg_seed=0x50B0A5)
        print("# wrote %d noisy TD anchors to %s" % (len(snrs), args.hw_dir))
        print("# anchor the HW:  make -C sim/soc run-ssb-fid SNR=<db>")

    # optional matplotlib plot (best-effort)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        xs = [r[0] for r in rows]
        ys = [max(r[1], 1e-6) for r in rows]
        plt.figure(figsize=(7, 4.5))
        plt.semilogy(xs, ys, "o-", color="#c0392b")
        plt.grid(True, which="both")
        plt.xlabel("SNR (dB)")
        plt.ylabel("MIB BER")
        plt.title("P1 SSB PBCH (Polar 512,32) AWGN curve")
        plot = os.path.splitext(out)[0] + ".png"
        plt.savefig(plot)
        print("# wrote %s" % plot)
    except Exception as ex:
        print("# (matplotlib not available, skipped plot: %s)" % ex)


if __name__ == "__main__":
    main()
