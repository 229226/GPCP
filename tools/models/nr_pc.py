#!/usr/bin/env python3
# --- tools path bootstrap (subdirs import root modules) ---
import os as _os, sys as _sys
_T = _os.path.dirname(_os.path.abspath(__file__))
_TOOLS = _T if _os.path.basename(_T) == "tools" else _os.path.dirname(_T)
for _p in (_TOOLS, _os.path.join(_TOOLS, "models"), _os.path.join(_TOOLS, "checks"),
           _os.path.join(_os.path.dirname(_TOOLS), "cgra", "tools")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
_ROOT = _os.path.dirname(_TOOLS)
# --- end bootstrap ---
"""Uplink closed-loop power control (38.213 7.1.1, simplified).

The gNB measures the received SINR, compares it with a target and sends a TPC
command (+1 / 0 / -1 dB); the UE accumulates it in f().  The open-loop part
uses the downlink path-loss estimate:

    P_PUSCH = P0 + alpha * PL + f(TPC)

What this model is for: proving that the loop converges from a large initial
error without oscillating, that the dead-band stops limit cycles, and that a
sign error in the TPC interpretation diverges (so the gate has teeth).
"""
import argparse
import os
import sys

P0_DBM = -90.0
ALPHA = 0.8
TARGET_SINR_DB = 10.0
TPC_STEP_DB = 1.0
DEAD_BAND_DB = 0.5
MAX_F_DB = 40.0


class PowerControl:
    def __init__(self, pathloss_db, f0_db=0.0):
        self.pl = pathloss_db
        self.f = f0_db

    def tx_power(self):
        """P_PUSCH in dBm (open loop + accumulated closed loop)."""
        return P0_DBM + ALPHA * self.pl + self.f

    def apply_tpc(self, delta_db):
        self.f = max(-MAX_F_DB, min(MAX_F_DB, self.f + delta_db))


def sinr_db(pc, noise_dbm):
    """Received SINR = tx power - path loss - noise figure."""
    return pc.tx_power() - pc.pl - noise_dbm


def tpc_from_sinr(measured_db, target_db=TARGET_SINR_DB):
    """gNB decision: +1 outside the dead band, -1 outside the other side."""
    err = target_db - measured_db
    if err > DEAD_BAND_DB:
        return 1
    if err < -DEAD_BAND_DB:
        return -1
    return 0


def closed_loop(pathloss_db, noise_dbm, iters=40, target_db=TARGET_SINR_DB,
                invert_tpc=False, f0_db=0.0):
    pc = PowerControl(pathloss_db, f0_db)
    hist, tpcs = [], []
    for _ in range(iters):
        s = sinr_db(pc, noise_dbm)
        hist.append(s)
        t = tpc_from_sinr(s, target_db)
        if invert_tpc:
            t = -t
        tpcs.append(t)
        pc.apply_tpc(t * TPC_STEP_DB)
    return pc, hist, tpcs


def selftest():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        if not cond:
            print("  " + msg)
            ok = False

    print("  P0=%.0f dBm alpha=%.1f target=%.1f dB step=%.0f dB dead-band=%.1f"
          % (P0_DBM, ALPHA, TARGET_SINR_DB, TPC_STEP_DB, DEAD_BAND_DB))

    for pl in (80.0, 100.0, 120.0):
        pc, hist, tpcs = closed_loop(pl, -100.0)
        final = hist[-1]
        settle = next((i for i, v in enumerate(hist)
                       if abs(v - TARGET_SINR_DB) <= DEAD_BAND_DB), None)
        print("  PL=%5.0f dB: start %+6.2f -> final %+6.2f dB, settled at %s, "
              "f=%+.1f dB" % (pl, hist[0], final, settle, pc.f))
        chk(abs(final - TARGET_SINR_DB) <= DEAD_BAND_DB + 0.01,
            "did not converge at PL=%.0f (final %.2f)" % (pl, final))
        chk(settle is not None and settle < 30, "convergence too slow at PL=%.0f" % pl)
        # no limit cycle: once settled, the TPC must stay inside the dead band
        tail = tpcs[settle + 2:] if settle is not None else []
        chk(all(t == 0 for t in tail), "limit cycle after settling at PL=%.0f" % pl)

    # large initial error must still converge
    pc, hist, _ = closed_loop(100.0, -100.0, f0_db=-15.0)
    print("  start -15 dB off: first %+6.2f -> final %+6.2f dB"
          % (hist[0], hist[-1]))
    chk(abs(hist[-1] - TARGET_SINR_DB) <= DEAD_BAND_DB + 0.01,
        "large initial error did not converge")

    # inverted TPC interpretation must diverge (the gate has teeth)
    pc, hist, _ = closed_loop(100.0, -100.0, invert_tpc=True)
    print("  inverted TPC: final %+6.2f dB (must run away from target)" % hist[-1])
    chk(abs(hist[-1] - TARGET_SINR_DB) > 3.0, "inverted TPC still converged")

    # f() must saturate rather than grow without bound
    pc = PowerControl(100.0)
    for _ in range(200):
        pc.apply_tpc(TPC_STEP_DB)
    chk(pc.f == MAX_F_DB, "f() did not saturate at %.0f dB" % MAX_F_DB)

    print("[ %s ] uplink closed-loop power control (38.213 7.1.1)"
          % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="uplink closed-loop power control")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("selftest")
    a = ap.parse_args()
    if a.cmd in (None, "selftest"):
        return selftest()
    return 2


if __name__ == "__main__":
    sys.exit(main())
