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
"""Link adaptation for the downlink data plane (P4 of docs/project/README.md).

Until now every PDSCH transmission used a fixed QPSK / MCS 17 regardless of
channel quality.  P4 makes the *control plane* choose the modulation and
coding scheme from a CSI report, and closes the loop with HARQ feedback:

    CSI-RS -> UE measures SINR -> CQI report (38.214 Table 5.2.2.1-2)
           -> gNB maps CQI to an MCS (38.214 Table 5.1.3.1-1)
           -> DCI carries the MCS (the project's 32-bit DCI container)
           -> PDSCH at that Qm / target code rate
           -> ACK/NACK drives the outer-loop link adaptation (OLLA) offset
           -> next CQI is adjusted so the long-run BLER converges to BLER_TARGET

The MCS and CQI tables below are the real 38.214 values (Table 5.1.3.1-1 for
PDSCH and Table 5.2.2.1-2 for 4-bit CQI, table 2 = up to 64QAM).  Each entry
is (Qm, R*1024); spectral efficiency = Qm * R / 1024.

The BLER model is an analytical AWGN approximation, not an actual LDPC run:
for each MCS a required SNR is obtained by inverting Shannon's bound with an
implementation gap, and BLER = Q((SNR - SNR_req)/SPREAD_DB).  That is enough
to make the *adaptation loop* verifiable and deterministic -- the actual coded
BLER for the hardware-verified QPSK chain is measured by the snr-sweep targets,
and P4-B/C extend the hardware to 16QAM.

Deliberate simplifications: no rank/PMI/CQI subband reporting (wideband single
CQI), no BLER look-up tables from the modem, no OLLA per HARQ process, no
MCS-CQI table switching by RRC (table 2 only), no 256QAM (table 3).
"""
import argparse
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# ---------------------------------------------------------------- MCS table
# 38.214 Table 5.1.3.1-1 (PDSCH MCS index table 1, up to 64QAM).
# (Qm, R*1024).  Indices 29..31 are reserved for retransmission and are not
# used here; the project's 5-bit mcs DCI field carries 0..28.
MCS_TABLE = [
    (2, 120), (2, 157), (2, 193), (2, 251), (2, 308),
    (2, 379), (2, 449), (2, 526), (2, 602), (2, 679),
    (4, 340), (4, 378), (4, 434), (4, 490), (4, 553),
    (4, 616), (4, 658),
    (6, 438), (6, 466), (6, 517), (6, 567), (6, 616),
    (6, 666), (6, 719), (6, 772), (6, 822), (6, 873),
    (6, 910), (6, 948),
]

# 38.214 Table 5.2.2.1-2 (4-bit CQI table 2).  Index 0 = out of range.
CQI_TABLE = [
    None,                       # 0: out of range
    (2, 78), (2, 120), (2, 193), (2, 308), (2, 449), (2, 602),
    (4, 378), (4, 490), (4, 616),
    (6, 466), (6, 567), (6, 666), (6, 772), (6, 873), (6, 948),
]

BLER_TARGET = 0.10
IMPL_GAP_DB = 3.0              # Shannon-gap / implementation loss
SPREAD_DB = 1.2                # BLER waterfall width (sigma)
OLLA_UP_DB = 0.1               # ACK: raise the reported SINR
OLLA_DOWN_DB = 0.9             # NACK: lower it (ratio 9 -> 10% BLER target)
MAX_MCS = len(MCS_TABLE) - 1


def eff_of(entry):
    """Spectral efficiency (bit/RE) of a (Qm, R*1024) table entry."""
    qm, r1024 = entry
    return qm * r1024 / 1024.0


def mcs_entry(mcs):
    if not 0 <= mcs <= MAX_MCS:
        raise ValueError("MCS %d outside 0..%d" % (mcs, MAX_MCS))
    return MCS_TABLE[mcs]


def cqi_entry(cqi):
    if not 1 <= cqi <= 15:
        raise ValueError("CQI %d outside 1..15" % cqi)
    return CQI_TABLE[cqi]


def qm_of_mcs(mcs):
    return MCS_TABLE[mcs][0]


def required_snr_db(entry):
    """SINR needed for this spectral efficiency (Shannon + implementation gap)."""
    eff = eff_of(entry)
    return 10.0 * math.log10(2.0 ** eff - 1.0) + IMPL_GAP_DB


def _q(x):
    """Q-function via erfc."""
    return 0.5 * math.erfc(x / math.sqrt(2.0))


def bler(mcs, snr_db):
    """Analytical BLER of one transmission at a given wideband SINR."""
    req = required_snr_db(mcs_entry(mcs))
    return _q((snr_db - req) / SPREAD_DB)


def cqi_from_sinr(sinr_db, offset_db=0.0):
    """UE wideband CQI report: highest CQI whose 10%-BLER SINR is met."""
    z = sinr_db + offset_db
    best = 0
    for cqi in range(1, 16):
        req = required_snr_db(cqi_entry(cqi)) + SPREAD_DB * 1.2816  # Q^-1(0.1)
        if z >= req:
            best = cqi
    return best


def mcs_from_cqi(cqi, olla_db=0.0):
    """gNB maps the reported CQI (+ OLLA offset) to an MCS."""
    if cqi == 0:
        return 0
    target_eff = eff_of(cqi_entry(cqi))
    # OLLA shifts the effective SINR, expressed here as a spectral-efficiency
    # margin: +1 dB ~ +0.33 bit/RE (Shannon slope at the operating point).
    target_eff += olla_db / 3.0
    best, bestdiff = 0, float("inf")
    for mcs in range(MAX_MCS + 1):
        d = eff_of(mcs_entry(mcs)) - target_eff
        if abs(d) < bestdiff:
            best, bestdiff = mcs, abs(d)
    return best


# ---------------------------------------------------------------- OLLA loop
class LinkAdaptation:
    """Stateful per-UE inner/outer loop link adaptation."""

    def __init__(self, target=BLER_TARGET):
        self.target = target
        self.olla_db = 0.0
        self.history = []

    def report(self, sinr_db):
        return cqi_from_sinr(sinr_db, self.olla_db)

    def select(self, sinr_db):
        cqi = self.report(sinr_db)
        mcs = mcs_from_cqi(cqi, self.olla_db)
        d = dict(cqi=cqi, mcs=mcs, qm=qm_of_mcs(mcs),
                 eff=eff_of(mcs_entry(mcs)),
                 pred_bler=bler(mcs, sinr_db), olla_db=self.olla_db)
        self.history.append(d)
        return d

    def feedback(self, ack):
        self.olla_db += OLLA_UP_DB if ack else -OLLA_DOWN_DB
        # keep the offset bounded so a long outage cannot drive it to -inf
        self.olla_db = max(-12.0, min(6.0, self.olla_db))
        return self.olla_db

    def step(self, sinr_db, ack=None):
        """One TTI: select, optionally consume the previous HARQ outcome."""
        if ack is not None:
            self.feedback(ack)
        return self.select(sinr_db)


def olla_target_offset():
    """The offset at which the ACK/NACK driven loop balances at BLER_TARGET."""
    # E[delta] = (1-b)*UP - b*DOWN = 0  ->  b = UP/(UP+DOWN)
    return OLLA_UP_DB / (OLLA_UP_DB + OLLA_DOWN_DB)


# ---------------------------------------------------------------- self test
def selftest():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        if not cond:
            print("  " + msg)
            ok = False

    print("Link adaptation (P4): 38.214 MCS/CQI tables, CSI report, "
          "OLLA, BLER target %.0f%%" % (BLER_TARGET * 100))

    # table sanity
    chk(len(MCS_TABLE) == 29 and len(CQI_TABLE) == 16,
        "table sizes wrong: MCS %d, CQI %d" % (len(MCS_TABLE), len(CQI_TABLE)))
    chk(all(MCS_TABLE[i][0] <= MCS_TABLE[i + 1][0] for i in range(28)),
        "MCS Qm not monotone")
    effs = [eff_of(e) for e in MCS_TABLE]
    # 38.214 Table 5.1.3.1-1 is NOT strictly monotone at the Qm boundary:
    # MCS 16 (16QAM, 658/1024) has eff 2.5703 while MCS 17 (64QAM, 438/1024)
    # has 2.5664.  Allow that tiny standard-table regression.
    chk(all(effs[i + 1] >= effs[i] * 0.99 for i in range(28)),
        "MCS spectral efficiency regressed by more than 1%")
    chk(effs[-1] > 20 * effs[0], "MCS table dynamic range implausible")
    chk(all(eff_of(CQI_TABLE[i]) <= eff_of(CQI_TABLE[i + 1]) * 1.001
            for i in range(1, 15)), "CQI efficiency not increasing")
    print("  MCS table: 29 entries, Qm %s, efficiency %.3f -> %.3f bit/RE"
          % (sorted(set(q for q, _ in MCS_TABLE)), effs[0], effs[-1]))

    # BLER waterfall is monotone in SNR and in MCS
    chk(all(bler(9, s) > bler(9, s + 1) for s in range(-5, 25)),
        "BLER not decreasing with SNR")
    # Same 16->17 table regression: require only "not dramatically easier".
    chk(all(bler(m, 12.0) <= bler(m + 1, 12.0) + 0.02 for m in range(28)),
        "a higher MCS became much easier")
    for mcs in (0, 9, 16, 28):
        req = required_snr_db(mcs_entry(mcs))
        print("    MCS %2d: Qm=%d eff=%.3f req_snr=%5.1f dB, "
              "BLER@req=%0.3f" % (mcs, MCS_TABLE[mcs][0], effs[mcs],
                                  req, bler(mcs, req)))
        chk(abs(bler(mcs, req) - 0.5) < 1e-9,
            "BLER at the required SNR should be 0.5")

    # CQI report rises with SINR and drives MCS up
    prev_cqi = prev_mcs = -1
    for sinr in (0, 5, 10, 15, 20, 25, 30):
        cqi = cqi_from_sinr(sinr)
        mcs = mcs_from_cqi(cqi)
        chk(cqi >= prev_cqi, "CQI decreased as SINR rose at %d dB" % sinr)
        chk(mcs >= prev_mcs, "MCS decreased as SINR rose at %d dB" % sinr)
        print("    SINR %2d dB -> CQI %2d -> MCS %2d (Qm=%d, eff=%.3f)"
              % (sinr, cqi, mcs, qm_of_mcs(mcs), eff_of(mcs_entry(mcs))))
        prev_cqi, prev_mcs = cqi, mcs
    chk(cqi_from_sinr(0) < cqi_from_sinr(30), "CQI did not adapt")
    chk(cqi_from_sinr(-10) == 0, "out-of-range SINR should report CQI 0")

    # the loop actually closes: at a fixed SNR, the ACK/NACK-driven OLLA
    # converges so that the realised BLER approaches BLER_TARGET
    import random
    rnd = random.Random(7)
    for sinr_db in (8.0, 14.0, 22.0):
        la = LinkAdaptation()
        nack = 0
        n = 2000
        for _ in range(n):
            d = la.select(sinr_db)
            ack = rnd.random() > bler(d["mcs"], sinr_db)
            la.feedback(ack)
            if not ack:
                nack += 1
        realised = nack / n
        saturated = all(x["mcs"] == MAX_MCS for x in la.history[-100:])
        if saturated:
            # No MCS above MAX_MCS exists, so OLLA cannot push BLER back up:
            # the link is capacity-limited, which is a valid (and desirable)
            # outcome.  Gate on full utilisation + BLER well under target.
            chk(realised < BLER_TARGET,
                "saturated at MCS %d but BLER %.3f >= target" % (MAX_MCS, realised))
            tag = "capacity-limited (MCS %d, BLER < target)" % MAX_MCS
        else:
            chk(abs(realised - BLER_TARGET) < 0.06,
                "OLLA BLER %.3f at %.0f dB, target %.2f"
                % (realised, sinr_db, BLER_TARGET))
            tag = "converged to BLER target"
        print("    OLLA @ %2.0f dB: realised BLER %.3f (target %.2f), "
              "final offset %+.2f dB -> %s"
              % (sinr_db, realised, BLER_TARGET, la.olla_db, tag))

    # injected faults must be visible
    bad = LinkAdaptation()
    bad.olla_db = 20.0
    chk(mcs_from_cqi(bad.report(10.0), bad.olla_db) == MAX_MCS,
        "a wildly positive OLLA should saturate MCS (injection not caught)")
    good = LinkAdaptation()
    chk(mcs_from_cqi(good.report(10.0)) != MAX_MCS,
        "baseline CQI at 10 dB should not saturate MCS")
    chk(abs(olla_target_offset() - BLER_TARGET) < 1e-9,
        "OLLA up/down ratio inconsistent with BLER target")
    print("  injection: bogus OLLA offset -> MCS saturation detected; "
          "up/down ratio %.3f matches BLER target" % olla_target_offset())

    # MCS fits the project's 5-bit DCI field
    chk(MAX_MCS < 32, "MCS does not fit the 5-bit DCI mcs field")
    print("  DCI fit: MCS 0..%d fits the 5-bit mcs field" % MAX_MCS)

    print("[ %s ] link adaptation (MCS/CQI + CSI + OLLA)" % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="downlink link adaptation")
    ap.add_argument("--snr", type=float, default=None)
    a = ap.parse_args()
    if a.snr is not None:
        la = LinkAdaptation()
        d = la.select(a.snr)
        print("SINR %.1f dB -> CQI %d, MCS %d (Qm %d, eff %.3f bit/RE), "
              "pred BLER %.3f" % (a.snr, d["cqi"], d["mcs"], d["qm"],
                                  d["eff"], d["pred_bler"]))
        return 0
    return selftest()


if __name__ == "__main__":
    raise SystemExit(main())
