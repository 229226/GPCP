#!/usr/bin/env python3
"""DCI-driven closed loop: MAC -> PDCCH -> PDSCH -> CRC -> PUCCH -> MAC.

This joins three chains that used to run in isolation (nr_pdcch.py,
pdsch_full_plan.py, nr_mac.py / nr_ul.py) into one controlled link:

    MAC SDU -> MAC PDU (10 bytes = 80 bit = A_TB) -> TB
      -> HarqEntity.dci_for(pid) -> DCI (32 bit)
      -> PDCCH transmit / RNTI blind detection      [run-pdcch on hardware]
      -> detected DCI *configures* the PDSCH transmission
      -> PDSCH (LDPC BG2, QPSK, Gold DMRS, 38.212 interleaving)  [run-pdsch-full]
      -> CRC24A -> ACK/NACK
      -> PUCCH format 0 feedback -> MAC -> new_tx (NDI toggles) or retx
      -> on failure the soft buffer is Chase-combined at the next RV's k0

What the DCI fields actually drive:
  * harq_pid : selects the per-process transport block and soft-combine buffer
  * ndi      : a toggle flushes the soft buffer (new transmission), else combine
  * rv       : genuinely selects the 38.212 5.4.2.1 circular-buffer offset k0
               (RV_K0 = {0:0, 1:246, 2:492, 3:738}), which is what makes a
               retransmission carry different redundancy bits
  * rnti     : genuinely drives the 38.211 7.3.3.1 PDSCH scrambling sequence
  * mcs / rb_start / rb_len / tpc : carried and checked field-by-field, but do
    NOT change modulation or allocation -- the physical link is fixed at QPSK,
    E=720 and 240 REs.  This is a deliberate simplification, not an omission.

Nothing here modifies the verified golden models: every function is imported
and reassembled, so run-pdcch / run-pdsch-full / run-pdsch-harq are untouched.
"""
import argparse
import os
import random
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import nr_mac as MAC
import nr_pdcch as PDCCH
import nr_ul as UL
import pdsch_plan as M
import pdsch_full_plan as F
import ldpc5g as L

A_TB = M.A_TB                       # 80 bits
USABLE = F.USABLE                   # 984 soft-buffer positions
RNTI = PDCCH.N_RNTI
CANDIDATES = PDCCH.CANDIDATES
PDCCH_SNR = 12.0                    # control channel is more robust (AL=4)
UL_SNR = 6.0                        # PUCCH format 0 detection SNR
MAX_TX = 4                          # 1 new transmission + up to 3 retx
DEFAULT_SNRS = (12.0, 0.0, -8.0)


# --------------------------------------------------------------- MAC / TB
def tb_from_sdu(sdu, lcid=1):
    """One SDU in a MAC PDU: 2-byte subheader + 8-byte payload = 80 bits."""
    assert len(sdu) == 8, "SDU must be 8 bytes so the PDU is exactly A_TB bits"
    pdu = MAC.pack_pdu([(lcid, sdu)])
    assert len(pdu) * 8 == A_TB
    bits = []
    for b in pdu:
        bits += [(b >> (7 - i)) & 1 for i in range(8)]
    return bits, pdu


def sdu_from_tb(tb):
    """Inverse of tb_from_sdu: only meaningful once CRC24A has passed."""
    by = bytearray()
    for i in range(0, A_TB, 8):
        v = 0
        for b in tb[i:i + 8]:
            v = (v << 1) | b
        by.append(v)
    return MAC.unpack_pdu(bytes(by))[0]


def make_sdu(seed):
    r = random.Random(seed)
    return bytes(r.randrange(256) for _ in range(8))


# ------------------------------------------------------------- PDCCH / DCI
def pdcch_step(dci, rnti, snr_db, seed):
    """Transmit one DCI and blind-detect it through a quantised OFDM symbol."""
    syms, _ = PDCCH.transmit(dci, rnti)
    td, _ = PDCCH.td_from_syms(syms, snr_db, seed)
    q = PDCCH.quantised_syms(td)
    return PDCCH.receive(q, CANDIDATES, rnti)


# ------------------------------------------------------------- PDSCH / TB
def tx_pdsch(tb, rv, rnti, snr_db, seed):
    """Encode one PDSCH transmission at the DCI-selected redundancy version."""
    cw = L.encode(tb + M.crc24a(tb)
                  + [0] * (M.KB * M.ZC - A_TB - M.CRC_LEN), M.KB, M.ZC)
    _, usable, filler = M.rate_match(cw)
    e_rv, k0 = F.rate_select(usable, rv)
    scr = M.scrambling_seq(rnti)
    e_sc = [b ^ s for b, s in zip(e_rv, scr)]
    f = F.interleave(e_sc)
    syms = M.qpsk_map(f)
    grid = [[0j] * F.NRE for _ in range(F.NSYM)]
    for s in range(F.NSYM):
        d = F.dmrs_seq(s)
        for p, r in enumerate(M.pilot_res()):
            grid[s][r] = d[p]
    n = 0
    for s in range(F.NSYM):
        for r in M.data_res():
            grid[s][r] = syms[n]
            n += 1
    y = M.add_channel(grid, snr_db, seed)
    return y, k0


def rx_pdsch(y, rnti):
    """Quantised TD round trip -> FFT -> Gold-DMRS LS -> equalise -> LLR."""
    yq = F.quantised_y(F.td_from_y(y))
    h = F.channel_estimate_fid(yq)
    raw = M.extract_llr(yq, h)
    llr_e = F.deinterleave(raw)
    scr = M.scrambling_seq(rnti)
    descr = [-v if scr[k] else v for k, v in enumerate(llr_e)]
    return descr, h


def combine(buf, descr, rv):
    """Chase accumulation in circular-buffer coordinates (38.212 5.4.2.1)."""
    k0 = F.RV_K0[rv]
    for k, v in enumerate(descr):
        buf[(k0 + k) % USABLE] += v


def decode(buf):
    """Turn the soft buffer into 1040 LDPC LLRs and run the hardware decoder."""
    dem = [0] * F.PUNCT
    u = 0
    for i in range(F.NCODED - F.PUNCT):
        if F.FILLER0 <= i < F.FILLER0 + F.FILLER_N:
            dem.append(0)
        else:
            dem.append(buf[u])
            u += 1
    assert len(dem) == F.NCODED
    hard, scaled = F.hw_decode(dem)
    tb = hard[:A_TB]
    crc = hard[A_TB:A_TB + M.CRC_LEN]
    return dict(tb=tb, crc_ok=(M.crc24a(tb) == crc), hard=hard, dem=dem)


# --------------------------------------------------------------- PUCCH
def pucch_step(ack, seed):
    y = UL.awgn(UL.pucch_tx(bool(ack)), UL_SNR, seed)
    return UL.pucch_detect(y)


# --------------------------------------------------------------- closed loop
def run_one_process(h, pid, sdu, snr_db, seed, pdcch_snr, max_tx):
    """Run one HARQ process to completion (or to max_tx failed attempts)."""
    tb, pdu = tb_from_sdu(sdu)
    dci = h.new_tx(pid, sdu)
    buf = None
    last_ndi = None
    rec = []
    for attempt in range(max_tx):
        seq_seed = seed * 1000 + pid * 64 + attempt
        det = pdcch_step(dci, RNTI, pdcch_snr, seq_seed)
        dci_ok = (det["ok"] and det["rnti"] == RNTI and det["fields"] == dci)
        hits = sum(1 for _, o in det["tried"] if o)
        # The receiver uses the *detected* DCI, not the transmitted one.
        f = det["fields"] if det["fields"] is not None else dci
        ndi, rv = f["ndi"], f["rv"]
        if buf is None or ndi != last_ndi:
            buf = [0] * USABLE
        last_ndi = ndi
        y, k0 = tx_pdsch(tb, rv, RNTI, snr_db, seq_seed)
        descr, h_est = rx_pdsch(y, RNTI)
        combine(buf, descr, rv)
        res = decode(buf)
        ack = 1 if res["crc_ok"] else 0
        ul = pucch_step(ack, seq_seed)
        tb_ok = res["tb"] == tb
        sdu_ok = True
        if res["crc_ok"]:
            try:
                sdu_ok = (sdu_from_tb(res["tb"]) == (1, sdu))
            except Exception:
                sdu_ok = False
        rec.append(dict(
            pid=pid, attempt=attempt, rv=rv, ndi=ndi, k0=k0,
            mcs=f["mcs"], rb_start=f["rb_start"], rb_len=f["rb_len"],
            dci_ok=dci_ok, rnti_hits=hits, crc_ok=res["crc_ok"],
            tb_ok=tb_ok, sdu_ok=sdu_ok, ack=ack, ul=ul,
            ul_ok=(ul == ack), chan=h_est,
            tb_errors=sum(a != b for a, b in zip(res["tb"], tb)),
        ))
        if res["crc_ok"]:
            break
        dci = h.retx(pid)
    return rec


def run_link(snr_db, trials=4, seed=1, pdcch_snr=PDCCH_SNR, max_tx=MAX_TX,
             verbose=True):
    h = MAC.HarqEntity()
    rec = []
    for t in range(trials):
        pid = t % MAC.NPROC
        sdu = make_sdu(seed * 100 + t)
        rec += run_one_process(h, pid, sdu, snr_db, seed, pdcch_snr, max_tx)
    n_success = sum(1 for r in rec if r["crc_ok"])
    n_retx = len(rec) - trials
    n_unresolved = trials - n_success
    # tb/sdu only carry meaning once CRC passes; a failed first transmission
    # legitimately has a wrong TB, so those records must not gate the loop.
    gates = dict(
        dci=all(r["dci_ok"] for r in rec),
        rnti=all(r["rnti_hits"] == 1 for r in rec),
        ul=all(r["ul_ok"] for r in rec),
        tb=all(r["tb_ok"] for r in rec if r["crc_ok"]),
        sdu=all(r["sdu_ok"] for r in rec if r["crc_ok"]),
        rv=all(r["rv"] == MAC.RV_CYCLE[r["attempt"]] for r in rec),
    )
    return dict(snr=snr_db, rec=rec, trials=trials, success=n_success,
                retx=n_retx, unresolved=n_unresolved, gates=gates,
                throughput=n_success * A_TB / max(1, len(rec)))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snr", type=float, default=None,
                    help="run one PDSCH SNR point instead of the sweep")
    ap.add_argument("--trials", type=int, default=4,
                    help="HARQ processes per SNR point")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--pdcch-snr", type=float, default=PDCCH_SNR)
    ap.add_argument("--max-tx", type=int, default=MAX_TX)
    args = ap.parse_args()

    snrs = (args.snr,) if args.snr is not None else DEFAULT_SNRS
    print("DCI-driven loop: A_TB=%d, RV=%s, RNTI=0x%04x, PDCCH %.1f dB, "
          "PUCCH %.1f dB" % (A_TB, MAC.RV_CYCLE, RNTI,
                             args.pdcch_snr, UL_SNR))
    ok = True
    results = []
    for snr in snrs:
        r = run_link(snr, args.trials, args.seed, args.pdcch_snr, args.max_tx)
        results.append(r)
        g = r["gates"]
        bad = [k for k, v in g.items() if not v]
        ok &= not bad
        print("  PDSCH %6.1f dB : %d/%d TB ok, %d retx, %d unresolved, "
              "%.1f bit/tx, gates %s" % (
                  snr, r["success"], r["trials"], r["retx"], r["unresolved"],
                  r["throughput"], "OK" if not bad else "BAD " + ",".join(bad)))
        for x in r["rec"]:
            print("    pid=%d attempt=%d rv=%d k0=%3d ndi=%d dci=%s "
                  "hits=%d crc=%s ack=%d/%s tb_err=%d" % (
                      x["pid"], x["attempt"], x["rv"], x["k0"], x["ndi"],
                      "OK" if x["dci_ok"] else "BAD", x["rnti_hits"],
                      "OK" if x["crc_ok"] else "BAD", x["ack"],
                      "OK" if x["ul_ok"] else "BAD", x["tb_errors"]))

    # A closed loop must actually close: retransmissions have to occur at a
    # low SNR, must eventually succeed, and must vanish at a high SNR.
    hi, lo = results[0], results[-1]
    if len(results) > 1 and hi["snr"] > lo["snr"]:
        closed = hi["retx"] == 0 and hi["unresolved"] == 0 and lo["retx"] > 0
        if not closed:
            print("  loop: expected 0 retx at %.1f dB and >0 retx at %.1f dB, "
                  "got %d / %d" % (hi["snr"], lo["snr"], hi["retx"], lo["retx"]))
            ok = False
        else:
            print("  loop: %.1f dB clean (0 retx) -> %.1f dB %d retx "
                  "(HARQ closes the link)" % (hi["snr"], lo["snr"], lo["retx"]))
    print("[ %s ] DCI-driven MAC/PDCCH/PDSCH/PUCCH loop" % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
