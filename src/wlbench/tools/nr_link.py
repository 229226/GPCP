#!/usr/bin/env python3
"""DCI-driven bidirectional closed loop: MAC -> PDCCH -> PDSCH/PUSCH -> MAC.

This joins the chains that used to run in isolation (nr_pdcch.py,
pdsch_full_plan.py, nr_pusch.py, nr_mac.py / nr_ul.py) into one controlled
**two-direction** link:

    Downlink (gNB -> UE)
      MAC SDU -> MAC PDU (10 bytes = 80 bit = A_TB) -> TB
        -> HarqEntity.dci_for(pid) -> DCI (32 bit)
        -> PDCCH transmit / RNTI blind detection      [run-pdcch on hardware]
        -> detected DCI *configures* the PDSCH transmission
        -> PDSCH (LDPC BG2, QPSK, Gold DMRS, 38.212 interleaving)  [run-pdsch-full]
        -> CRC24A -> ACK/NACK -> PUCCH format 0 -> MAC (NDI toggles) or retx
        -> on failure the soft buffer is Chase-combined at the next RV's k0

    Uplink (UE -> gNB)
      MAC SDU -> MAC PDU -> TB
        -> gNB builds a DCI whose reserved[9:0] carry an **UL grant**
           (ul_flag / ul_ndi / ul_rv / ul_mcs / ul_rb_start / ul_rb_len)
        -> PDCCH transmit / blind detection -> UE extracts the grant
        -> UE transmits PUSCH at the granted RV      [nr_pusch.py model]
        -> gNB receives: FFT -> PUSCH Gold DMRS LS -> equalise -> QPSK LLR
           -> de-interleave -> descramble -> rate de-match -> LDPC -> CRC24A
        -> on failure the gNB advances ul_rv and grants the retransmission

What the DCI fields actually drive:
  * harq_pid : selects the per-process transport block and soft-combine buffer
  * ndi      : a toggle flushes the soft buffer (new transmission), else combine
  * rv       : genuinely selects the 38.212 5.4.2.1 circular-buffer offset k0
               (RV_K0 = {0:0, 1:246, 2:492, 3:738}), which is what makes a
               retransmission carry different redundancy bits
  * rnti     : genuinely drives the 38.211 7.3.3.1 PDSCH scrambling sequence
  * reserved : on the uplink DCI, carries the 10-bit UL grant; ul_rv drives the
               PUSCH k0 the same way rv drives the PDSCH k0, and ul_ndi flushes
               the uplink soft buffer -- so the uplink is DCI-driven too
  * mcs / rb_start / rb_len / tpc : carried and checked field-by-field, but do
    NOT change modulation or allocation -- the physical link is fixed at QPSK,
    E=720 and 240 REs.  This is a deliberate simplification, not an omission.

The DCI 32-bit layout is untouched (tools/nr_pdcch.py DCI_FIELDS unchanged);
the UL grant rides in the pre-existing 10 reserved bits, so run-pdcch /
run-link-e2e and their hardware models are not affected.

Nothing here modifies the verified golden models: every function is imported
and reassembled, so nr_pdcch / pdsch_full_plan / nr_pusch / nr_ul / run-* are
untouched.
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
import nr_pusch as P
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

# ---- UL grant carried in the DCI reserved bits (10 bits) -------------------
# 38.213-style compact uplink grant: enough to drive ndi / rv / MCS / RB fields
# through the control channel without touching the 32-bit DCI layout.
UL_GRANT_FIELDS = [("ul_flag", 1), ("ul_ndi", 1), ("ul_rv", 2),
                   ("ul_mcs", 2), ("ul_rb_start", 2), ("ul_rb_len", 2)]
UL_GRANT_BITS = sum(w for _, w in UL_GRANT_FIELDS)     # 10


def pack_ul_grant(g):
    """Pack a UL grant dict into the 10 reserved DCI bits (MSB first)."""
    v = 0
    for name, w in UL_GRANT_FIELDS:
        v = (v << w) | (int(g.get(name, 0)) & ((1 << w) - 1))
    return v


def unpack_ul_grant(v):
    """Inverse of pack_ul_grant."""
    out = {}
    for name, w in reversed(UL_GRANT_FIELDS):
        out[name] = v & ((1 << w) - 1)
        v >>= w
    return out


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


# ------------------------------------------------------------- PUSCH / TB
def tx_pusch(tb, rv, rnti, snr_db, seed, tp=False):
    """UE-side PUSCH transmission at the granted redundancy version."""
    grid = P.build_pusch_grid_tb(tb, rv, tp, rnti)
    y = M.add_channel(grid, snr_db, seed)
    return y, P.RV_K0[rv]


def rx_pusch(y, rnti, tp=False):
    """gNB-side PUSCH reception: FFT -> 6.4.1.1 DMRS LS -> LLR -> descramble."""
    yq = P.quantised_y(P.td_from_y(y))
    h = P.pusch_channel_estimate(yq)
    raw = P.pusch_extract_llr(yq, h, tp)
    llr_e = P.deinterleave(raw)
    scr = P.pusch_scrambling_seq(rnti)
    descr = [-v if scr[k] else v for k, v in enumerate(llr_e)]
    return descr, h


# ------------------------------------------------------------- soft buffer
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


# --------------------------------------------------------------- DL process
def run_dl_process(h, pid, sdu, snr_db, seed, pdcch_snr, max_tx):
    """Run one downlink HARQ process to completion (or to max_tx attempts)."""
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


# --------------------------------------------------------------- UL process
def run_ul_process(pid, sdu, snr_db, seed, pdcch_snr, max_tx, tp=False):
    """Run one uplink HARQ process: DCI grant -> PUSCH -> CRC -> next ul_rv."""
    tb, pdu = tb_from_sdu(sdu)
    grant = dict(ul_flag=1, ul_ndi=(pid & 1), ul_rv=0, ul_mcs=0,
                 ul_rb_start=0, ul_rb_len=8)
    buf = None
    last_ndi = None
    rec = []
    for attempt in range(max_tx):
        seq_seed = seed * 1000 + 500 + pid * 64 + attempt
        # gNB -> UE: a DCI whose reserved bits carry the uplink grant.
        dci = dict(mcs=0, rv=0, ndi=0, harq_pid=pid, rb_start=0, rb_len=0,
                   tpc=0, reserved=pack_ul_grant(grant))
        det = pdcch_step(dci, RNTI, pdcch_snr, seq_seed)
        dci_ok = (det["ok"] and det["rnti"] == RNTI and det["fields"] == dci)
        hits = sum(1 for _, o in det["tried"] if o)
        # The UE acts on the *detected* grant, not the intended one.
        g = unpack_ul_grant(det["fields"]["reserved"]) if det["fields"] else dict(grant)
        ndi, rv = g["ul_ndi"], g["ul_rv"]
        if buf is None or ndi != last_ndi:
            buf = [0] * USABLE
        last_ndi = ndi
        y, k0 = tx_pusch(tb, rv, RNTI, snr_db, seq_seed, tp)
        descr, h_est = rx_pusch(y, RNTI, tp)
        combine(buf, descr, rv)
        res = decode(buf)
        tb_ok = res["tb"] == tb
        sdu_ok = True
        if res["crc_ok"]:
            try:
                sdu_ok = (sdu_from_tb(res["tb"]) == (1, sdu))
            except Exception:
                sdu_ok = False
        rec.append(dict(
            pid=pid, attempt=attempt, rv=rv, ndi=ndi, k0=k0,
            ul_mcs=g["ul_mcs"], ul_rb_start=g["ul_rb_start"],
            ul_rb_len=g["ul_rb_len"], dci_ok=dci_ok, rnti_hits=hits,
            crc_ok=res["crc_ok"], tb_ok=tb_ok, sdu_ok=sdu_ok, chan=h_est,
            tb_errors=sum(a != b for a, b in zip(res["tb"], tb)),
        ))
        if res["crc_ok"]:
            break
        # gNB schedules the retransmission at the next RV (keeps ul_ndi).
        nxt = MAC.RV_CYCLE[(MAC.RV_CYCLE.index(grant["ul_rv"]) + 1)
                           % len(MAC.RV_CYCLE)]
        grant = dict(grant)
        grant["ul_rv"] = nxt
    return rec


# --------------------------------------------------------------- closed loop
def run_one_process(h, pid, dl_sdu, ul_sdu, snr_db, seed, pdcch_snr, max_tx,
                    tp=False):
    """Run one downlink and one uplink HARQ process (separate TBs)."""
    dl = run_dl_process(h, pid, dl_sdu, snr_db, seed, pdcch_snr, max_tx)
    ul = run_ul_process(pid, ul_sdu, snr_db, seed, pdcch_snr, max_tx, tp)
    return dl, ul


def run_link(snr_db, trials=4, seed=1, pdcch_snr=PDCCH_SNR, max_tx=MAX_TX,
             tp=False, verbose=True):
    h = MAC.HarqEntity()
    dl_all, ul_all = [], []
    for t in range(trials):
        pid = t % MAC.NPROC
        dl_sdu = make_sdu(seed * 100 + t)
        ul_sdu = make_sdu(seed * 1000 + t + 7)      # distinct UL payload
        dl, ul = run_one_process(h, pid, dl_sdu, ul_sdu, snr_db, seed,
                                 pdcch_snr, max_tx, tp)
        dl_all += dl
        ul_all += ul
    dl_ok = sum(1 for r in dl_all if r["crc_ok"])
    ul_ok = sum(1 for r in ul_all if r["crc_ok"])
    dl_retx = len(dl_all) - trials
    ul_retx = len(ul_all) - trials
    dl_unres = trials - dl_ok
    ul_unres = trials - ul_ok
    # tb/sdu only carry meaning once CRC passes; a failed first transmission
    # legitimately has a wrong TB, so those records must not gate the loop.
    gates = dict(
        dci=all(r["dci_ok"] for r in dl_all + ul_all),
        rnti=all(r["rnti_hits"] == 1 for r in dl_all + ul_all),
        dl_tb=all(r["tb_ok"] for r in dl_all if r["crc_ok"]),
        dl_sdu=all(r["sdu_ok"] for r in dl_all if r["crc_ok"]),
        dl_rv=all(r["rv"] == MAC.RV_CYCLE[r["attempt"]] for r in dl_all),
        ul_tb=all(r["tb_ok"] for r in ul_all if r["crc_ok"]),
        ul_sdu=all(r["sdu_ok"] for r in ul_all if r["crc_ok"]),
        ul_rv=all(r["rv"] == MAC.RV_CYCLE[r["attempt"]] for r in ul_all),
        ul_k0=all(r["k0"] == r["rv"] * (USABLE // 4) for r in ul_all),
    )
    return dict(snr=snr_db, dl=dl_all, ul=ul_all, trials=trials,
                dl_success=dl_ok, ul_success=ul_ok,
                dl_retx=dl_retx, ul_retx=ul_retx,
                dl_unresolved=dl_unres, ul_unresolved=ul_unres,
                gates=gates,
                throughput=(dl_ok + ul_ok) * A_TB
                           / max(1, len(dl_all) + len(ul_all)))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snr", type=float, default=None,
                    help="run one PDSCH/PUSCH SNR point instead of the sweep")
    ap.add_argument("--trials", type=int, default=4,
                    help="HARQ processes per SNR point")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--pdcch-snr", type=float, default=PDCCH_SNR)
    ap.add_argument("--max-tx", type=int, default=MAX_TX)
    ap.add_argument("--tp", action="store_true",
                    help="use DFT-s-OFDM transform precoding on the uplink")
    args = ap.parse_args()

    snrs = (args.snr,) if args.snr is not None else DEFAULT_SNRS
    print("DCI-driven bidirectional loop: A_TB=%d, RV=%s, RNTI=0x%04x, "
          "PDCCH %.1f dB, PUCCH %.1f dB, UL grant %d bits in DCI reserved%s"
          % (A_TB, MAC.RV_CYCLE, RNTI, args.pdcch_snr, UL_SNR,
             UL_GRANT_BITS, ", DFT-s-OFDM" if args.tp else ""))
    ok = True
    results = []
    for snr in snrs:
        r = run_link(snr, args.trials, args.seed, args.pdcch_snr, args.max_tx,
                     args.tp)
        results.append(r)
        g = r["gates"]
        bad = [k for k, v in g.items() if not v]
        ok &= not bad
        print("  %6.1f dB : DL %d/%d TB ok, %d retx, %d unres | "
              "UL %d/%d TB ok, %d retx, %d unres | %.1f bit/attempt, gates %s"
              % (snr, r["dl_success"], r["trials"], r["dl_retx"],
                 r["dl_unresolved"], r["ul_success"], r["trials"],
                 r["ul_retx"], r["ul_unresolved"], r["throughput"],
                 "OK" if not bad else "BAD " + ",".join(bad)))
        for x in r["dl"]:
            print("    DL pid=%d attempt=%d rv=%d k0=%3d ndi=%d dci=%s "
                  "hits=%d crc=%s ack=%d/%s tb_err=%d" % (
                      x["pid"], x["attempt"], x["rv"], x["k0"], x["ndi"],
                      "OK" if x["dci_ok"] else "BAD", x["rnti_hits"],
                      "OK" if x["crc_ok"] else "BAD", x["ack"],
                      "OK" if x["ul_ok"] else "BAD", x["tb_errors"]))
        for x in r["ul"]:
            print("    UL pid=%d attempt=%d ul_rv=%d k0=%3d ul_ndi=%d dci=%s "
                  "hits=%d crc=%s tb_err=%d" % (
                      x["pid"], x["attempt"], x["rv"], x["k0"], x["ndi"],
                      "OK" if x["dci_ok"] else "BAD", x["rnti_hits"],
                      "OK" if x["crc_ok"] else "BAD", x["tb_errors"]))

    # A closed loop must actually close in both directions: retransmissions
    # have to occur at low SNR and vanish at high SNR, in DL and UL alike.
    hi, lo = results[0], results[-1]
    if len(results) > 1 and hi["snr"] > lo["snr"]:
        dl_closed = hi["dl_retx"] == 0 and hi["dl_unresolved"] == 0 and lo["dl_retx"] > 0
        ul_closed = hi["ul_retx"] == 0 and hi["ul_unresolved"] == 0 and lo["ul_retx"] > 0
        if not (dl_closed and ul_closed):
            print("  loop: expected 0 retx at %.1f dB and >0 retx at %.1f dB; "
                  "DL %d/%d, UL %d/%d" % (hi["snr"], lo["snr"], hi["dl_retx"],
                                          lo["dl_retx"], hi["ul_retx"], lo["ul_retx"]))
            ok = False
        else:
            print("  loop: %.1f dB clean (DL/UL 0 retx) -> %.1f dB "
                  "DL %d retx, UL %d retx (HARQ closes both directions)"
                  % (hi["snr"], lo["snr"], lo["dl_retx"], lo["ul_retx"]))
    print("[ %s ] DCI-driven bidirectional MAC/PDCCH/PDSCH/PUSCH loop"
          % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
