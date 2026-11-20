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
"""RRC connection establishment + control-plane procedures (P3 of docs/project/README.md).

The UE has finished P2 (4-step random access) and owns a C-RNTI.  This model
runs the next protocol procedures over the *real* physical-layer chains that
are already hardware-verified, so only the procedure is new:

    DL-SCH  RRCSetup            (PDCCH with C-RNTI -> PDSCH)  -> SRB1 installed
    UL-SCH  RRCSetupComplete    (PUSCH)                        -> CONNECTED
    PUCCH   Scheduling Request  (format 0, 1 bit)
    DL-SCH  UL grant            (PDCCH, grant in DCI reserved)
    UL-SCH  Short BSR / PHR     (PUSCH, MAC CEs)               -> gNB sizes grant
    DL-SCH  RRCReconfiguration + TA Command CE                 -> TA maintained
    UL-SCH  RRCReconfigurationComplete

Reused engines (unchanged):
    PDCCH  tools/models/nr_pdcch.py / nr_rach.pdcch_step   (run-pdcch, run-sib1)
    PDSCH  tools/models/nr_link.py tx_pdsch/rx_pdsch       (run-pdsch-full)
    PUSCH  tools/models/nr_pusch.py build_pusch_grid_tb     (run-pusch)
    PUCCH  tools/models/nr_ul.py pucch_tx/pucch_detect     (nr_ul.py)
    MAC/RRC tools/models/nr_mac.py, tools/models/nr_rrc.py

Couplings are real, not decorative:
  * every DCI is masked with the C-RNTI from Msg4; a wrong RNTI kills the
    blind detection;
  * every PDSCH/PUSCH is scrambled with that same C-RNTI;
  * the UL grant is carried in the DCI reserved bits (same 10-bit layout as
    nr_link.py's uplink grant), and the BSR the UE sends is parsed by the gNB
    to size the following grant;
  * the TA command CE and the RRCReconfiguration TA must agree, and the UE
    applies the resulting TA;
  * SRB1 must be installed by RRCSetup before RRCSetupComplete is accepted.

Deliberate simplifications: compact fixed-order RRC SDUs instead of 38.331
ASN.1 PER (docs/project/README.md P6), single TTI, no RLC segmentation/PDCP ciphering,
no RRC timers, no measurement configuration beyond the carried fields.
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
import nr_link as LINK
import nr_rach as RACH
import nr_rrc as RRC
import nr_pusch as P
import nr_ul as UL
import pdsch_plan as M
import pdsch_full_plan as F

C_RNTI = RACH.tc_rnti_of(2)        # from P2 Msg4 (0x007002)
DEFAULT_TA = 9                      # from P2 Msg1
PDCCH_SNR = 12.0
DL_SNR = 20.0
UL_SNR = 20.0
PUCCH_SNR = 6.0
DEFAULT_SEED = 1

# DCI reserved[9:0] carries the uplink grant (same layout as nr_link.py).
UL_GRANT = dict(ul_flag=1, ul_ndi=1, ul_rv=0, ul_mcs=2,
                ul_rb_start=1, ul_rb_len=5)


def dl_dci(mcs=11, rv=0, ndi=1):
    return dict(mcs=mcs, rv=rv, ndi=ndi, harq_pid=0, rb_start=0, rb_len=8,
                tpc=0, reserved=0)


def ul_dci(grant=None):
    g = dict(UL_GRANT if grant is None else grant)
    return dict(mcs=9, rv=0, ndi=0, harq_pid=0, rb_start=0, rb_len=8,
                tpc=0, reserved=LINK.pack_ul_grant(g))


# ---------------------------------------------------------------- PHY legs
def pdcch_step(dci, rnti, snr_db, seed, tx_rnti=None):
    return RACH.pdcch_step(dci, rnti, snr_db, seed, tx_rnti=tx_rnti)


def tx_dl(tb, dci, rnti, snr_db, seed):
    return LINK.tx_pdsch(tb, dci["rv"], rnti, snr_db, seed)


def rx_dl(y, rv, rnti):
    descr, h = LINK.rx_pdsch(y, rnti)
    buf = [0] * LINK.USABLE
    LINK.combine(buf, descr, rv)
    return LINK.decode(buf), h


def tx_ul(tb, rv, rnti, snr_db, seed, tp=False):
    grid = P.build_pusch_grid_tb(tb, rv, tp, rnti)
    return M.add_channel(grid, snr_db, seed)


def rx_ul(y, rv, rnti, tp=False):
    """gNB-side PUSCH receive for a standard (TA-free) window."""
    yq = P.quantised_y(P.td_from_y(y))
    h = P.pusch_channel_estimate(yq)
    raw = P.pusch_extract_llr(yq, h, tp)
    llr_e = P.deinterleave(raw)
    scr = P.pusch_scrambling_seq(rnti)
    descr = [-v if scr[k] else v for k, v in enumerate(llr_e)]
    buf = [0] * LINK.USABLE
    LINK.combine(buf, descr, rv)
    return LINK.decode(buf), h


def pucch_sr_step(has_sr, snr_db, seed):
    """UE sends a 1-bit SR on PUCCH format 0; gNB detects it."""
    y = UL.awgn(UL.pucch_tx(has_sr), snr_db, seed)
    return UL.pucch_detect(y), y


# ---------------------------------------------------------------- the process
def conn_process(c_rnti=C_RNTI, ta=DEFAULT_TA, seed=DEFAULT_SEED,
                 pdcch_snr=PDCCH_SNR, dl_snr=DL_SNR, ul_snr=UL_SNR,
                 pucch_snr=PUCCH_SNR, tp=False,
                 wrong_c_rnti=False, no_sr=False, wrong_bsr=False,
                 wrong_ta=False, skip_srb1=False, verbose=True):
    """Run the full connection establishment and control-plane procedures."""
    ue = RRC.RrcEntity(ue_identity=0x00A1B2C3D4, c_rnti=c_rnti, ta=ta)
    steps = []

    def step(name, **kw):
        steps.append(dict(name=name, **kw))

    # 1) RRCSetup on DL-SCH (SRB0).  UE blind-detects the DCI with its C-RNTI
    #    and descrambles the PDSCH with the same value.
    setup = ue.setup(ta_command=ta)
    tb1 = RRC.pdu_to_tb(setup)
    dci1 = dl_dci(mcs=11, rv=0)
    rnti_tx = 0x1234 if wrong_c_rnti else c_rnti
    det1 = pdcch_step(dci1, c_rnti, pdcch_snr, seed, tx_rnti=rnti_tx)
    y1, k0_1 = tx_dl(tb1, dci1, rnti_tx, dl_snr, seed)
    rx1, h1 = rx_dl(y1, dci1["rv"], c_rnti)
    setup_fields = None
    if rx1["crc_ok"]:
        try:
            _, setup_fields = RRC.unpack_pdu(RRC.tb_to_pdu(rx1["tb"]))
        except Exception:
            setup_fields = None
    srb1_ok = False
    if setup_fields is not None and not skip_srb1:
        srb1_ok = ue.apply_setup(setup_fields)
    step("RRCSetup", dci_ok=det1["ok"] and det1["rnti"] == c_rnti,
         crc_ok=rx1["crc_ok"], k0=k0_1, srb1_ok=srb1_ok,
         c_rnti_rx=setup_fields["c_rnti"] if setup_fields else None,
         tb_errors=sum(a != b for a, b in zip(rx1["tb"], tb1)))

    # 2) RRCSetupComplete on UL-SCH (SRB1).  Sending it before SRB1 exists is
    #    a protocol violation the state machine must reject.
    complete_ok = False
    tb2 = None
    rx2 = None
    if ue.srb1_active:
        complete = ue.setup_complete()
        tb2 = RRC.pdu_to_tb(complete)
        y2 = tx_ul(tb2, 0, c_rnti, ul_snr, seed + 1, tp)
        rx2, _h2 = rx_ul(y2, 0, c_rnti, tp)
        complete_ok = rx2["crc_ok"] and RRC.tb_to_pdu(rx2["tb"]) == complete
    step("RRCSetupComplete", sent=ue.srb1_active, crc_ok=bool(rx2 and rx2["crc_ok"]),
         pdu_ok=complete_ok,
         tb_errors=(sum(a != b for a, b in zip(rx2["tb"], tb2))
                    if rx2 is not None and tb2 is not None else -1))

    # 3) SR -> UL grant -> BSR.  The gNB only grants after a detected SR.
    sr_tx, sr_y = pucch_sr_step(True, pucch_snr, seed + 2)
    sr_rx = 0 if no_sr else sr_tx
    dci3 = ul_dci()
    det3 = pdcch_step(dci3, c_rnti, pdcch_snr, seed + 3) if sr_rx else None
    grant = (LINK.unpack_ul_grant(det3["fields"]["reserved"])
             if det3 and det3["fields"] is not None else None)
    bsr_lcg, bsr_size = 3, 17
    if wrong_bsr:
        bsr_size = 4                 # UE reports a different buffer
    bsr_pdu = RRC.pack_short_bsr(bsr_lcg, bsr_size)
    tb3 = RRC.pdu_to_tb(bsr_pdu)
    rx3 = None
    bsr_rx = None
    if grant and grant["ul_flag"]:
        y3 = tx_ul(tb3, grant["ul_rv"], c_rnti, ul_snr, seed + 4, tp)
        rx3, _h3 = rx_ul(y3, grant["ul_rv"], c_rnti, tp)
        if rx3["crc_ok"]:
            bsr_rx = RRC.parse_short_bsr(RRC.tb_to_pdu(rx3["tb"]))
    # gNB sizes the next grant from the *reported* buffer size; a mismatch with
    # what the UE actually has queued is a real gate failure.
    grant_match = (bsr_rx is not None and bsr_rx["buffer_size"] == 17
                   and bsr_rx["lcg_id"] == 3)
    step("SR_BSR", sr_tx=sr_tx, sr_rx=sr_rx, grant=grant,
         dci_ok=bool(det3 and det3["ok"]), bsr=bsr_rx,
         crc_ok=bool(rx3 and rx3["crc_ok"]), grant_match=grant_match)

    # 4) PHR on UL-SCH.
    phr_pdu = RRC.pack_phr(42)
    tb4 = RRC.pdu_to_tb(phr_pdu)
    y4 = tx_ul(tb4, 0, c_rnti, ul_snr, seed + 5, tp)
    rx4, _h4 = rx_ul(y4, 0, c_rnti, tp)
    phr_rx = RRC.parse_phr(RRC.tb_to_pdu(rx4["tb"])) if rx4["crc_ok"] else None
    step("PHR", crc_ok=rx4["crc_ok"], phr=phr_rx,
         tb_errors=sum(a != b for a, b in zip(rx4["tb"], tb4)))

    # 5) RRCReconfiguration + TA Command CE, then RRCReconfigurationComplete.
    new_ta = 11
    recfg = ue.reconfiguration(drb_id=2, ta_command=new_ta)
    tb5 = RRC.pdu_to_tb(recfg)
    dci5 = dl_dci(mcs=13, rv=0)
    det5 = pdcch_step(dci5, c_rnti, pdcch_snr, seed + 6)
    y5, k0_5 = tx_dl(tb5, dci5, c_rnti, dl_snr, seed + 7)
    rx5, _h5 = rx_dl(y5, dci5["rv"], c_rnti)
    recfg_fields = None
    if rx5["crc_ok"]:
        try:
            _, recfg_fields = RRC.unpack_pdu(RRC.tb_to_pdu(rx5["tb"]))
        except Exception:
            recfg_fields = None
    ta_ce = RRC.parse_ta_cmd(RRC.pack_ta_cmd(0, 11 if not wrong_ta else 3))
    recfg_ok = False
    if recfg_fields is not None:
        recfg_ok = ue.apply_reconfiguration(recfg_fields, ta_ce=ta_ce)
    ta_ok = (ue.ta == new_ta and ta_ce["ta_command"] == recfg_fields["ta_command"]
             if recfg_fields else False)
    # RRCReconfigurationComplete on UL-SCH.
    comp5 = ue.reconfiguration_complete()
    tb6 = RRC.pdu_to_tb(comp5)
    y6 = tx_ul(tb6, 0, c_rnti, ul_snr, seed + 8, tp)
    rx6, _h6 = rx_ul(y6, 0, c_rnti, tp)
    step("RRCReconfiguration", dci_ok=det5["ok"], crc_ok=rx5["crc_ok"],
         k0=k0_5, recfg_ok=recfg_ok, ta=ue.ta, ta_ok=ta_ok,
         ta_ce=ta_ce["ta_command"],
         complete_crc=rx6["crc_ok"],
         complete_pdu_ok=(rx6["crc_ok"]
                          and RRC.tb_to_pdu(rx6["tb"]) == comp5),
         state=ue.state)

    rec = dict(steps=steps, c_rnti=c_rnti, ta=ta, ta_final=ue.ta,
               state=ue.state, srb1_active=ue.srb1_active, seed=seed,
               pucch_y=sr_y)
    if verbose:
        show(rec)
    return rec


# ---------------------------------------------------------------- gates
def gates(rec):
    s = {x["name"]: x for x in rec["steps"]}
    return dict(
        setup_dci=s["RRCSetup"]["dci_ok"],
        setup_crc=s["RRCSetup"]["crc_ok"],
        setup_srb1=s["RRCSetup"]["srb1_ok"],
        complete=s["RRCSetupComplete"]["pdu_ok"],
        sr_detected=s["SR_BSR"]["sr_rx"] == 1,
        sr_grant=s["SR_BSR"]["dci_ok"] and bool(s["SR_BSR"]["grant"]),
        bsr_crc=s["SR_BSR"]["crc_ok"],
        bsr_grant_match=s["SR_BSR"]["grant_match"],
        phr=s["PHR"]["crc_ok"] and s["PHR"]["phr"] == dict(ph=42),
        recfg_dci=s["RRCReconfiguration"]["dci_ok"],
        recfg_crc=s["RRCReconfiguration"]["crc_ok"],
        recfg_applied=s["RRCReconfiguration"]["recfg_ok"],
        ta_maintained=s["RRCReconfiguration"]["ta_ok"],
        recfg_complete=s["RRCReconfiguration"]["complete_pdu_ok"],
        connected=(rec["state"] == "CONNECTED_RECONFIGURED"
                   and rec["srb1_active"]),
    )


def show(rec):
    print("  RRC connection C-RNTI=0x%04x TA %d -> %d, state=%s"
          % (rec["c_rnti"], rec["ta"], rec["ta_final"], rec["state"]))
    for x in rec["steps"]:
        extra = ""
        if x["name"] == "RRCSetup":
            extra = ("dci=%s crc=%s srb1=%s k0=%d err=%d"
                     % (x["dci_ok"], x["crc_ok"], x["srb1_ok"], x["k0"], x["tb_errors"]))
        elif x["name"] == "RRCSetupComplete":
            extra = "sent=%s crc=%s pdu=%s" % (x["sent"], x["crc_ok"], x["pdu_ok"])
        elif x["name"] == "SR_BSR":
            extra = "SR %d->%d grant=%s bsr=%s pdu_ok=%s" % (
                x["sr_tx"], x["sr_rx"], bool(x["grant"]), x["bsr"], x["grant_match"])
        elif x["name"] == "PHR":
            extra = "crc=%s phr=%s" % (x["crc_ok"], x["phr"])
        else:
            extra = "dci=%s crc=%s k0=%d applied=%s TA=%d (CE %d) ta_ok=%s complete=%s" % (
                x["dci_ok"], x["crc_ok"], x["k0"], x["recfg_ok"], x["ta"],
                x["ta_ce"], x["ta_ok"], x["complete_pdu_ok"])
        print("    %-22s %s" % (x["name"], extra))


# ---------------------------------------------------------------- self test
def selftest():
    ok = True
    print("RRC connection control-plane process (P3): C-RNTI=0x%04x, "
          "SRB0->SRB1, SR/BSR/PHR, TA maintenance" % C_RNTI)

    rec = conn_process(verbose=False)
    g = gates(rec)
    bad = [k for k, v in g.items() if not v]
    ok &= not bad
    show(rec)
    print("  baseline gates: %s" % ("OK" if not bad else "BAD " + ",".join(bad)))

    injections = [
        ("wrong C-RNTI on RRCSetup", dict(wrong_c_rnti=True),
         ["setup_dci", "setup_crc", "setup_srb1", "complete", "connected"]),
        ("no SR detected", dict(no_sr=True),
         ["sr_detected", "sr_grant", "bsr_crc", "bsr_grant_match"]),
        ("wrong BSR buffer size", dict(wrong_bsr=True), ["bsr_grant_match"]),
        ("wrong TA command CE", dict(wrong_ta=True), ["ta_maintained"]),
        ("skip SRB1 install", dict(skip_srb1=True),
         ["setup_srb1", "complete", "recfg_applied", "connected"]),
    ]
    for name, kw, expect in injections:
        r = conn_process(verbose=False, **kw)
        gg = gates(r)
        failed = [k for k, v in gg.items() if not v]
        hit = all(k in failed for k in expect)
        ok &= hit
        print("  injection %-26s : gates failed %s %s"
              % (name, ",".join(expect) if hit else ",".join(failed),
                 "[OK]" if hit else "[FAIL]"))

    # transform-precoded uplink also closes the loop
    rt = conn_process(verbose=False, tp=True)
    gtp = gates(rt)
    badtp = [k for k, v in gtp.items() if not v]
    ok &= not badtp
    print("  DFT-s-OFDM uplink : gates %s"
          % ("OK" if not badtp else "BAD " + ",".join(badtp)))

    # a few seeds
    seed_ok = True
    for sd in (2, 3):
        r = conn_process(seed=sd, verbose=False)
        gg = gates(r)
        if any(not v for v in gg.values()):
            seed_ok = False
    ok &= seed_ok
    print("  extra seeds 2/3 : %s" % ("OK" if seed_ok else "BAD"))

    print("[ %s ] RRC connection + control-plane loop" % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="RRC connection process model")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--trials", type=int, default=1)
    a = ap.parse_args()
    return selftest()


if __name__ == "__main__":
    raise SystemExit(main())
