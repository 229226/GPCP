#!/usr/bin/env python3
"""4-step random access golden model: Msg1 PRACH -> Msg2 RAR -> Msg3 -> Msg4.

This is Phase P2 of docs/11-项目计划.md.  The UE has read MIB + SIB1
(run-ssb-fid / run-sib1) and now tries to get onto the air:

    Msg1  PRACH preamble : ZC root, cyclic shift = preamble*N_CS + TA
                           -> gNB detects (preamble index, timing advance)
    Msg2  RAR            : DCI 1_0 masked by RA-RNTI -> PDSCH -> RAR MAC PDU
                           (BI, RAPID, TA command, 27-bit UL grant, TC-RNTI)
    Msg3  RRCSetupRequest: PUSCH scrambled with the *decoded* TC-RNTI, received
                           on the timing derived from Msg1
    Msg4  contention resolution
                         : DCI 1_0 masked by TC-RNTI -> PDSCH -> UE Contention
                           Resolution Identity CE compared against the CCCH SDU
                           the UE actually sent -> TC-RNTI becomes the C-RNTI
                           (38.321 5.1.5.3)

Every leg reuses an engine that is already hardware-verified, so only the
*process* is new here:

    PDCCH  tools/nr_pdcch.py  (run-pdcch / run-sib1)
    PDSCH  tools/nr_link.py   tx_pdsch / rx_pdsch / combine / decode
    PUSCH  tools/nr_pusch.py  (run-pusch), addressed by TC-RNTI here
    MAC    tools/nr_mac.py    pack_rar / pack_cr_pdu / HarqEntity

The couplings are real, not decorative:
  * RA-RNTI is derived from the PRACH occasion (38.321 6.1.5) and masks the DCI
    CRC, so Msg2 is only decodable when the occasion coordinates agree;
  * the TA *command* in the RAR equals the TA *measured* from Msg1, and the
    Msg3 receive window is placed with that measured value (38.213 8.3: Msg3
    reception timing derives from the detected preamble timing);
  * the TC-RNTI out of the RAR drives the 38.211 6.3.1.1 PUSCH scrambling of
    Msg3 and the CRC mask of Msg4's DCI;
  * Msg4's contention-resolution identity is checked against the CCCH SDU the
    UE sent, so a corrupted Msg3 leaves the contention unresolved.

Deliberate simplifications (all of them listed here):
  * L_RA = 139 chips (short ZC sequence, as in tools/nr_ul.py) and a single root
    sequence.  The preamble index comes from the correlation peak position
    modulo the zero-correlation zone N_CS -- exactly what a real receiver does -
    so only L_RA//N_CS (9 for N_CS=15) of the 64 preambles are reachable.
  * N_CS comes from the SIB1 zeroCorrelationZoneConfig through a short table
    (format-0 low entries only); prach-ConfigurationIndex is carried and checked
    but the full 38.211 6.3.3.2 occasion tables are not implemented: the
    coordinates (s_id, t_id, f_id) are derived from msg1-FDM and the occasion
    index.
  * the Msg3 RRCSetupRequest is a compact fixed-order 64-bit profile, not
    38.331 ASN.1 PER -- real RRC encoding is P3's job.
  * no preamble power ramping, no ra-ResponseWindow timer, no Msg3 transform
    precoding.  Msg3 is single-shot at rv=0 as 38.321 5.1.3 requires; Msg4 runs
    a real HARQ entity (NDI + 0->2->3->1 RV cycle + Chase combining).

Constants below MUST stay in sync with sw/soc/prach_bm.c and
sw/soc/rach_bm.c (run-prach / run-rach, docs/11 P2 acceptance).
"""
import argparse
import math
import os
import random
import struct
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import nr_mac as MAC
import nr_pdcch as PDCCH
import nr_link as LINK
import nr_pusch as P
import nr_sib1 as S1
import nr_ul as UL
import pdsch_plan as M
import pdsch_full_plan as F
from ssb_plan import _fft

# ---------------------------------------------------------------- Msg1 / PRACH
L_RA = UL.L_RA                     # 139 ZC chips
ROOT_U = UL.ROOT_U
PRACH_TAPS = 127                   # taps per array run (the correlate kernel)
CHIP_SCALE = 2048                  # int16 headroom for chips and reference taps
MAX_TA = 16                        # receive-window room, must exceed N_CS
MSG3_ROW = 2 * P.SYM_LEN + 2 * MAX_TA   # samples per Msg3 row: see pusch_td_timed

# 38.211 zero-correlation-zone config -> N_CS in chips (preamble format 0,
# non-restricted set; only the low entries are modelled).  The benchmark uses
# index 0 -> N_CS = 15, giving L_RA // 15 = 9 preambles per root.
NCS_FROM_ZCZC = (15, 18, 22, 26, 32, 38, 46, 54, 66, 80, 90, 110)

# 38.321 Table 6.2.3-1: backoff indicator -> backoff in ms (low entries only).
BACKOFF_MS = (0, 5, 10, 20, 30, 40, 60, 80, 120, 160, 240, 320, 480)

# ------------------------------------------------------------------ SNR points
PRACH_SNR = 6.0
PDCCH_SNR = 12.0
MS2_SNR = 20.0                     # RAR PDSCH
MS3_SNR = 20.0                     # Msg3 PUSCH
MS4_SNR = 20.0                     # Msg4 PDSCH
DEFAULT_SEED = 1
MAX_MS4_TX = 4

RA_RAR_INDICATOR_BIT = 0           # DCI reserved[0] = 1 -> schedules a RAR


def n_cs_of(sib1):
    """Zero-correlation-zone config from the SIB1 profile -> N_CS in chips."""
    zczc = int(sib1.get("zero_corr_zone", 0))
    if zczc >= len(NCS_FROM_ZCZC):
        raise ValueError("zero_corr_zone=%d is above the modelled N_CS table" % zczc)
    return NCS_FROM_ZCZC[zczc]


def preambles_per_root(n_cs):
    return L_RA // n_cs


# ------------------------------------------------------- RA-RNTI / RA occasion
def ra_occasion(sib1, occ):
    """Compact PRACH occasion -> (s_id, t_id, f_id) for the RA-RNTI."""
    n_fdm = 1 << (int(sib1.get("msg1_fdm", 0)) & 3)      # msg1-FDM n1/n2/n4/n8
    f_id = occ % n_fdm
    rest = occ // n_fdm
    return (rest // 10) % 14, rest % 10, f_id


def ra_rnti(s_id, t_id, f_id, ul_ci=0):
    """38.321 6.1.5: RA-RNTI = 1 + s_id + 6*t_id + 60*f_id + 240*ul_ci."""
    return 1 + s_id + 6 * t_id + 60 * f_id + 240 * ul_ci


def tc_rnti_of(preamble):
    """A deterministic TC-RNTI for the benchmark (16-bit PHY RNTI width)."""
    return 0x7000 | (preamble & 0x3F)


# --------------------------------------------------------- Msg1 transmit / detect
def zc_chips(u=ROOT_U, n=L_RA):
    """Quantised ZC root: the integer taps the CGRA correlates against."""
    return [(int(round(c.real * CHIP_SCALE)), int(round(c.imag * CHIP_SCALE)))
            for c in UL.zc_root(u, n)]


def msg1_tx(preamble, ta, n_cs, chips, snr_db=PRACH_SNR, seed=DEFAULT_SEED):
    """Received Msg1 chips: cyclic shift preamble*N_CS + TA, plus AWGN."""
    shift = preamble * n_cs + ta
    if shift >= L_RA:
        raise ValueError("preamble %d with TA %d leaves the correlation window"
                         % (preamble, ta))
    y = [chips[(i + shift) % L_RA] for i in range(L_RA)]
    rnd = random.Random(seed)
    sigma = CHIP_SCALE / math.sqrt(2 * 10 ** (snr_db / 10))
    return [(int(round(v[0] + rnd.gauss(0, sigma))),
             int(round(v[1] + rnd.gauss(0, sigma)))) for v in y]


def msg1_correlations(y, chips, taps=PRACH_TAPS):
    """(re, im) for every candidate shift d -- the kernel's exact integer sum."""
    out = []
    for d in range(L_RA):
        re = im = 0
        for i in range(taps):
            yr, yi = y[i]
            cr, ci = chips[(i + d) % L_RA]
            re += yr * cr + yi * ci          # real{ sum y * conj(c) }
            im += yi * cr - yr * ci          # imag{ sum y * conj(c) }
        out.append((re, im))
    return out


def msg1_detect(y, chips, n_cs, taps=PRACH_TAPS):
    """Integer cyclic-shift search -- the exact arithmetic of the CGRA kernel.

    For candidate shift d the array computes

        sum_i y[i] * conj(root[(i + d) mod L_RA]),  i = 0 .. taps-1

    whose peak lands on d = preamble*N_CS + TA; the zero-correlation-zone rule
    splits d back into the preamble index and the timing advance.
    """
    peaks = sorted(((r * r + i * i, r, i, d)
                    for d, (r, i) in enumerate(msg1_correlations(y, chips, taps))),
                   key=lambda p: -p[0])
    best, second = peaks[0], peaks[1]
    return dict(delay=best[3], power=best[0], re=best[1], im=best[2],
                preamble=best[3] // n_cs, ta=best[3] % n_cs,
                second_power=second[0], second_delay=second[3],
                margin=best[0] / max(1, second[0]))


# ----------------------------------------------------------- 10-byte PDU <-> TB
def pdu_to_tb(pdu):
    """80-bit transport block (MSB first) from a 10-byte MAC PDU."""
    assert len(pdu) * 8 == M.A_TB, "PDU must be exactly %d bytes" % (M.A_TB // 8)
    bits = []
    for b in pdu:
        bits += [(b >> (7 - i)) & 1 for i in range(8)]
    return bits


def tb_to_pdu(bits):
    by = bytearray()
    for i in range(0, M.A_TB, 8):
        v = 0
        for b in bits[i:i + 8]:
            v = (v << 1) | b
        by.append(v)
    return bytes(by)


# ------------------------------------------------------- Msg3 / Msg4 payloads
# Compact RRCSetupRequest as the Msg3 CCCH SDU (8 bytes = 64 bits).
MSG3_FIELDS = [("establishment_cause", 4), ("ue_identity", 40),
               ("preamble_used", 6), ("reserved", 14)]
MSG3_BITS = sum(w for _, w in MSG3_FIELDS)               # 64
CAUSE_MO_SIGNALLING = 4

RAR_DCI = dict(mcs=11, rv=0, ndi=0, harq_pid=0, rb_start=0, rb_len=8, tpc=0,
               reserved=(1 << RA_RAR_INDICATOR_BIT))
RAR_GRANT = dict(fh=0, rb_start=1, rb_len=5, tda=2, mcs=10, harq_timing=3,
                 tpc=1, power_class=1, reserved=0)
RAR_BI = 3
MS4_PID = 1


def pack_msg3_sdu(ue_identity, preamble, cause=CAUSE_MO_SIGNALLING):
    v = 0
    for name, x in (("establishment_cause", cause), ("ue_identity", ue_identity),
                    ("preamble_used", preamble), ("reserved", 0)):
        w = dict(MSG3_FIELDS)[name]
        if not 0 <= x < (1 << w):
            raise ValueError("%s=%d does not fit in %d bits" % (name, x, w))
        v = (v << w) | x
    return v.to_bytes(MSG3_BITS // 8, "big")


def unpack_msg3_sdu(sdu):
    v = int.from_bytes(sdu, "big")
    out = {}
    for name, w in reversed(MSG3_FIELDS):
        out[name] = v & ((1 << w) - 1)
        v >>= w
    return out


# ------------------------------------- PUSCH whose receive window is timed by TA
def pusch_td_timed(y, ta_true):
    """Msg3 time-domain blocks: the ideal waveform arriving ``ta_true`` late.

    Per symbol the row is 2*SYM_LEN + 2*MAX_TA samples: the true symbol, a
    MAX_TA-wide cyclic extension in front of it, and *the following symbol* of
    the same allocation behind it.  So a receive window that stays inside the
    cyclic prefix sees a phase rotation only (and the DMRS-based per-subcarrier
    equaliser absorbs it -- real physics), while a window pushed past the prefix
    lands on the next symbol's data and destroys the decode.  That is what makes
    the Msg1 timing estimate load-bearing.
    """
    freqs = []
    for s in range(P.NSYM):
        freq = [0j] * P.NFFT
        for r in range(P.NRE):
            freq[P.bin_of(r)] = y[s][r]
        freqs.append(_fft(freq, inverse=True))          # 1/N normalised IFFT
    span = MSG3_ROW
    out = []
    for s in range(P.NSYM):
        cur = freqs[s]
        nxt = freqs[(s + 1) % P.NSYM]
        off = MAX_TA + ta_true + P.CP_LEN       # first useful sample of symbol s
        row = []
        for p in range(span):
            if p < off + P.NFFT:
                c = cur[(p - off) % P.NFFT]
            else:
                q = (p - (off + P.NFFT)) % P.SYM_LEN   # inside the next symbol
                c = nxt[P.NFFT - P.CP_LEN + q] if q < P.CP_LEN else nxt[q - P.CP_LEN]
            row.append(complex(int(round(c.real)), int(round(c.imag))))
        out.append(row)
    return out


def pusch_rx_timed(td, rnti, ta_window, tp=False):
    """gNB-side Msg3 receive: the FFT starts CP_LEN + ta_window into each block."""
    base = MAX_TA + ta_window
    yq = []
    for s in range(P.NSYM):
        spec = _fft(td[s][base + P.CP_LEN:base + P.CP_LEN + P.NFFT], inverse=False)
        yq.append([spec[P.bin_of(r)] for r in range(P.NRE)])
    h = P.pusch_channel_estimate(yq)
    raw = P.pusch_extract_llr(yq, h, tp)
    llr_e = P.deinterleave(raw)
    scr = P.pusch_scrambling_seq(rnti)
    descr = [-v if scr[k] else v for k, v in enumerate(llr_e)]
    return descr, h, yq


def pdcch_step(dci, rnti, snr_db, seed, tx_rnti=None):
    """One DCI transmission, blind-detected against *this* RNTI only.

    nr_link.pdcch_step searches the five C-RNTI candidates of run-pdcch; random
    access is scheduled by RNTIs the UE computes itself (RA-RNTI from the PRACH
    occasion, TC-RNTI from the RAR), so the candidate list is that one RNTI --
    the same single-candidate shape run-sib1 uses for the SI-RNTI.  ``tx_rnti``
    masks the CRC with a different RNTI than the receiver tries, which is how the
    self-test injects a wrong TC-RNTI.
    """
    syms, _ = PDCCH.transmit(dci, rnti if tx_rnti is None else tx_rnti)
    td, _ = PDCCH.td_from_syms(syms, snr_db, seed)
    return PDCCH.receive(PDCCH.quantised_syms(td), [rnti], rnti)


# ------------------------------------------------------------------ the process
def rach_process(sib1=None, preamble=2, ta_true=9, occ=1, ue_identity=None,
                 prach_snr=PRACH_SNR, pdcch_snr=PDCCH_SNR, ms2_snr=MS2_SNR,
                 ms3_snr=MS3_SNR, ms4_snr=MS4_SNR, seed=DEFAULT_SEED,
                 max_ms4_tx=MAX_MS4_TX, force_ta=None, msg4_rnti_tx=None,
                 msg4_rv=None, contend_with=None, verbose=True):
    """Run one 4-step random access attempt and return every stage's record.

    ``force_ta`` overrides the TA the Msg3 receiver uses and ``msg4_rnti_tx``
    makes the gNB mask Msg4's DCI with a different RNTI; both exist so the
    self-test can prove the couplings are load-bearing.  ``msg4_rv`` sends the
    *first* Msg4 transmission at a non-zero redundancy version (run-rach uses it
    to check that the hardware reads k0 out of the decoded DCI, as rv=0 would
    leave k0=0 and prove nothing).  ``contend_with`` is the CCCH SDU of *another*
    UE that grabbed the same preamble: the gNB decodes that one and echoes it, so
    the CE arrives intact but does not match our identity.
    """
    sib1 = dict(S1.default_sib1()) if sib1 is None else dict(sib1)
    n_cs = n_cs_of(sib1)
    chips = zc_chips()

    # ---- Msg1: preamble -> (preamble index, timing advance)
    y1 = msg1_tx(preamble, ta_true, n_cs, chips, prach_snr, seed)
    det1 = msg1_detect(y1, chips, n_cs)
    s_id, t_id, f_id = ra_occasion(sib1, occ)
    ra = ra_rnti(s_id, t_id, f_id)

    # ---- Msg2: RA-RNTI-scheduled PDSCH carrying the RAR.  Its TA command and
    #      TC-RNTI are built from what the gNB *detected*, never from ta_true.
    rar_pdu = MAC.pack_rar(rapid=det1["preamble"], ta=det1["ta"],
                           ul_grant=dict(RAR_GRANT),
                           tc_rnti=tc_rnti_of(det1["preamble"]), bi=RAR_BI)
    tb2 = pdu_to_tb(rar_pdu)
    dci2 = dict(RAR_DCI)
    det2 = pdcch_step(dci2, ra, pdcch_snr, seed)
    y2, k0_2 = LINK.tx_pdsch(tb2, dci2["rv"], ra, ms2_snr, seed)
    d2, h2 = LINK.rx_pdsch(y2, ra)
    buf2 = [0] * LINK.USABLE
    LINK.combine(buf2, d2, dci2["rv"])
    rx2 = LINK.decode(buf2)
    rar = MAC.parse_rar(tb_to_pdu(rx2["tb"])) if rx2["crc_ok"] else None
    tc = rar["tc_rnti"] if rar else 0

    # ---- Msg3: PUSCH scrambled by the decoded TC-RNTI, timed by Msg1
    sdu3 = pack_msg3_sdu(0x1234ABCD00 + preamble if ue_identity is None
                         else ue_identity, preamble)
    tb3 = pdu_to_tb(MAC.pack_pdu([(MAC.LCID_CCCH, sdu3)]))
    grid3 = P.build_pusch_grid_tb(tb3, 0, False, tc)      # 38.321: Msg3 is rv=0
    k0_3 = P.RV_K0[0]
    y3 = M.add_channel(grid3, ms3_snr, seed + 1)
    td3 = pusch_td_timed(y3, ta_true)
    ta_window = det1["ta"] if force_ta is None else force_ta
    d3, h3, _yq3 = pusch_rx_timed(td3, tc, ta_window)
    buf3 = [0] * LINK.USABLE
    LINK.combine(buf3, d3, 0)
    rx3 = LINK.decode(buf3)
    rx_sdu3 = None
    try:
        rx_sdu3 = next((p for l, p in MAC.unpack_pdu(tb_to_pdu(rx3["tb"]))
                        if l == MAC.LCID_CCCH), None)
    except Exception:
        rx_sdu3 = None
    sdu3_rx = rx_sdu3 if rx3["crc_ok"] else None

    # ---- Msg4: TC-RNTI masks the DCI, the CE carries the identity back.  The
    #      gNB echoes what it actually *received* on Msg3, CRC or not, so a
    #      corrupted Msg3 leaves the contention unresolved at the UE
    #      (38.321 5.1.5) and the attempt restarts after the backoff.
    ce_src = (rx_sdu3 if rx_sdu3 is not None else sdu3) if contend_with is None \
        else contend_with
    cr_pdu = MAC.pack_cr_pdu(ce_src)
    tb4 = pdu_to_tb(cr_pdu)
    rnti_tx = tc if msg4_rnti_tx is None else msg4_rnti_tx
    h = MAC.HarqEntity()
    msg4 = []
    resolved = False
    c_rnti = None
    dci4 = h.new_tx(MS4_PID, cr_pdu, mcs=11)
    if msg4_rv is not None:
        dci4["rv"] = msg4_rv
    buf4, last_ndi = None, None
    for attempt in range(max_ms4_tx):
        seq_seed = seed * 100 + attempt
        # The UE blind-detects Msg4 with *its* TC-RNTI; the gNB masked the CRC
        # with rnti_tx, so a wrong msg4_rnti_tx shows up as a failed detection.
        det4 = pdcch_step(dci4, tc, pdcch_snr, seq_seed, tx_rnti=rnti_tx)
        f4 = det4["fields"] if det4["fields"] is not None else dci4
        if buf4 is None or f4["ndi"] != last_ndi:
            buf4 = [0] * LINK.USABLE
        last_ndi = f4["ndi"]
        y4, k0_4 = LINK.tx_pdsch(tb4, f4["rv"], rnti_tx, ms4_snr, seq_seed)
        # The UE descrambles Msg4 with *its* TC-RNTI, out of the decoded RAR.
        d4, _h4 = LINK.rx_pdsch(y4, tc)
        LINK.combine(buf4, d4, f4["rv"])
        rx4 = LINK.decode(buf4)
        ce = MAC.parse_cr_pdu(tb_to_pdu(rx4["tb"])) if rx4["crc_ok"] else None
        # The UE compares the CE against the identity *it* sent in Msg3.
        match = ce is not None and ce == sdu3[:MAC.CR_ID_BITS // 8]
        msg4.append(dict(attempt=attempt, rv=f4["rv"], ndi=f4["ndi"], k0=k0_4,
                         dci_ok=det4["ok"] and det4["rnti"] == tc,
                         rnti=det4["rnti"], crc_ok=rx4["crc_ok"], ce=ce,
                         match=match,
                         tb_errors=sum(a != b for a, b in zip(rx4["tb"], tb4))))
        if attempt == 0:
            y4_first, dci4_first = y4, dict(dci4)
        if rx4["crc_ok"] and match and det4["ok"] and det4["rnti"] == tc:
            resolved = True
            c_rnti = tc
            break
        if rx4["crc_ok"] and not match:
            break                       # contention lost: restart from Msg1
        dci4 = h.retx(MS4_PID, mcs=11)

    rec = dict(sib1=sib1, n_cs=n_cs, n_preambles=preambles_per_root(n_cs),
               preamble_tx=preamble, ta_true=ta_true, occ=occ,
               occ_ids=(s_id, t_id, f_id), ra_rnti=ra, msg1=det1,
               dci2=dci2, dci2_rx=det2["fields"], dci2_ok=det2["ok"],
               dci2_rnti=det2["rnti"], rar_pdu=rar_pdu, rar=rar,
               msg2_crc=rx2["crc_ok"], msg2_tb_errors=sum(
                   a != b for a, b in zip(rx2["tb"], tb2)), k0_2=k0_2,
               chan2=h2, sdu3=sdu3, sdu3_rx=sdu3_rx, rx_sdu3=rx_sdu3,
               msg3_crc=rx3["crc_ok"], msg3_k0=k0_3,
               msg3_tb_errors=sum(a != b for a, b in zip(rx3["tb"], tb3)),
               msg3_h=h3, ta_window=ta_window, tc_rnti=tc, rnti_tx=rnti_tx,
               ce_src=ce_src,
               msg4=msg4, resolved=resolved, c_rnti=c_rnti,
               msg4_retx=len(msg4) - 1,
               seed=seed, prach_snr=prach_snr, pdcch_snr=pdcch_snr,
               ms2_snr=ms2_snr, ms3_snr=ms3_snr, ms4_snr=ms4_snr,
               y1=y1, chips=chips, y2=y2, td3=td3, tb2=tb2, tb3=tb3, tb4=tb4,
               y4=y4_first, dci4=dci4_first, msg4_rv=dci4_first["rv"],
               msg4_ndi=dci4_first["ndi"])
    if verbose:
        show(rec)
    return rec


def backoff_ms(bi):
    return BACKOFF_MS[bi] if bi is not None and bi < len(BACKOFF_MS) else None


def show(rec):
    m1 = rec["msg1"]
    print("  Msg1 : preamble %d/%d TA %2d/%2d (delay %3d, peak %.3g, 2nd %.3g, "
          "margin %.1f dB)" % (m1["preamble"], rec["preamble_tx"], m1["ta"],
                               rec["ta_true"], m1["delay"], m1["power"],
                               m1["second_power"],
                               10 * math.log10(max(1e-9, m1["margin"]))))
    print("  RA-RNTI: occ(s%d,t%d,f%d) -> 0x%04x" % (
        rec["occ_ids"] + (rec["ra_rnti"],)))
    rar = rec["rar"]
    print("  Msg2 : dci=%s crc=%s TA=%d TC-RNTI=0x%04x BI=%d grant=%s" % (
        "OK" if rec["dci2_ok"] and rec["dci2_rnti"] == rec["ra_rnti"] else "BAD",
        "OK" if rec["msg2_crc"] else "BAD",
        rar["ta"] if rar else -1, rar["tc_rnti"] if rar else 0,
        rar["bi"] if rar else -1, rar["ul_grant"] if rar else None))
    print("  Msg3 : window TA=%d crc=%s sdu=%s" % (
        rec["ta_window"], "OK" if rec["msg3_crc"] else "BAD",
        "OK" if rec["sdu3_rx"] == rec["sdu3"] else "BAD"))
    for r in rec["msg4"]:
        print("  Msg4 : attempt=%d rv=%d ndi=%d k0=%3d dci=%s crc=%s CE=%s "
              "tb_err=%d" % (r["attempt"], r["rv"], r["ndi"], r["k0"],
                             "OK" if r["dci_ok"] else "BAD",
                             "OK" if r["crc_ok"] else "BAD",
                             "match" if r["match"] else "no", r["tb_errors"]))
    if rec["resolved"]:
        print("  -> contention resolved: TC-RNTI 0x%04x becomes the C-RNTI"
              % rec["c_rnti"])
    else:
        print("  -> NOT resolved; backoff %s ms then restart from Msg1"
              % backoff_ms(rar["bi"] if rar else None))


# --------------------------------------------------------------------- gates
def gates(rec):
    m1, rar = rec["msg1"], rec["rar"]
    want_rar = MAC.pack_rar(rapid=m1["preamble"], ta=m1["ta"],
                            ul_grant=dict(RAR_GRANT),
                            tc_rnti=tc_rnti_of(m1["preamble"]), bi=RAR_BI)
    last = rec["msg4"][-1]
    return dict(
        msg1_preamble=m1["preamble"] == rec["preamble_tx"],
        msg1_ta=m1["ta"] == rec["ta_true"],
        msg2_dci=rec["dci2_ok"] and rec["dci2_rnti"] == rec["ra_rnti"]
                 and rec["dci2_rx"] == rec["dci2"],
        msg2_crc=rec["msg2_crc"],
        msg2_pdu=rec["rar_pdu"] == want_rar and rar is not None,
        msg2_rapid=rar is not None and rar["rapid"] == rec["preamble_tx"],
        msg2_ta_cmd=rar is not None and rar["ta"] == m1["ta"],
        msg2_tc_rnti=rar is not None and rar["tc_rnti"] == tc_rnti_of(
            rec["preamble_tx"]),
        msg2_grant=rar is not None and rar["ul_grant"] == dict(RAR_GRANT),
        msg3_crc=rec["msg3_crc"],
        msg3_sdu=rec["sdu3_rx"] == rec["sdu3"],
        msg3_k0=rec["msg3_k0"] == 0,
        msg4_dci=all(r["dci_ok"] for r in rec["msg4"]),
        msg4_crc=last["crc_ok"],
        msg4_match=last["match"],
        resolved=rec["resolved"],
        c_rnti=rec["c_rnti"] == rec["tc_rnti"],
    )


# --------------------------------------------------------------------- TD dumps
# The SoC loads two images: the PRACH stimulus and the four message legs.  All
# offsets are 32-bit words (one complex sample = 2 words) from the start of the
# image, and sw/soc/prach_bm.c / sw/soc/rach_bm.c mirror them exactly.
def prach_offsets():
    return dict(y=0, refs=2 * L_RA,
                refs_words=2 * L_RA * PRACH_TAPS)


def msg_offsets():
    sym = P.SYM_LEN
    timed = MSG3_ROW
    o = {}
    o["ms2_pdcch"] = 0
    o["ms2_pdsch"] = o["ms2_pdcch"] + 2 * sym
    o["ms3_pusch"] = o["ms2_pdsch"] + 2 * P.NSYM * sym
    o["ms4_pdcch"] = o["ms3_pusch"] + 2 * P.NSYM * timed
    o["ms4_pdsch"] = o["ms4_pdcch"] + 2 * sym
    o["total"] = o["ms4_pdsch"] + 2 * P.NSYM * sym
    o["sym_words"] = 2 * sym
    o["timed_sym_words"] = 2 * timed
    return o


def _pack_complex(seq, scale=1):
    out = bytearray()
    for c in seq:
        out += struct.pack("<I", (int(c.real) * scale) & 0xFFFFFFFF)
        out += struct.pack("<I", (int(c.imag) * scale) & 0xFFFFFFFF)
    return out


def _pack_pairs(pairs, scale=1):
    out = bytearray()
    for re, im in pairs:
        out += struct.pack("<I", re * scale & 0xFFFFFFFF)
        out += struct.pack("<I", im * scale & 0xFFFFFFFF)
    return out


def dump_prach(path, y, chips):
    """[ received Msg1 chips | L_RA rotated 127-tap references ] for run-prach."""
    out = _pack_pairs(y)
    for d in range(L_RA):
        out += _pack_pairs([chips[(i + d) % L_RA] for i in range(PRACH_TAPS)])
    with open(path, "wb") as f:
        f.write(out)
    return len(out)


def msg_blocks(rec):
    """The four message legs in dump order, rebuilt from the record.

    Every leg comes out of the *record*, so the time-domain image and the golden
    anchors cannot drift apart: the RAR PDU is the one the gNB built from its own
    Msg1 detection, Msg3 is scrambled with the TC-RNTI the RAR carries, and Msg4
    is masked with the same RNTI and received with the UE's own.
    """
    blocks = []
    syms, _ = PDCCH.transmit(rec["dci2"], rec["ra_rnti"])
    td, _ = PDCCH.td_from_syms(syms, rec["pdcch_snr"], rec["seed"])
    blocks.append(("ms2_pdcch", [td]))
    blocks.append(("ms2_pdsch", F.td_from_y(rec["y2"])))
    blocks.append(("ms3_pusch", rec["td3"]))
    syms4, _ = PDCCH.transmit(rec["dci4"], rec["rnti_tx"])
    td4, _ = PDCCH.td_from_syms(syms4, rec["pdcch_snr"], rec["seed"] * 100)
    blocks.append(("ms4_pdcch", [td4]))
    blocks.append(("ms4_pdsch", F.td_from_y(rec["y4"])))
    return blocks


def dump_msgs(path, rec):
    out = bytearray()
    for _name, sym_blocks in msg_blocks(rec):
        for blk in sym_blocks:
            out += _pack_complex(blk, P.TD_SCALE)
    with open(path, "wb") as f:
        f.write(out)
    return len(out)


def build_all(preamble, ta_true, occ, args, sib1=None):
    """One RACH attempt plus whichever time-domain image was asked for.

    run-prach only needs ``prach.td`` (Msg1), which costs a correlation; the
    full ``rach.td`` requires the whole four-step process, so it is only built
    when ``--msg-out`` is given.
    """
    sib1 = dict(S1.default_sib1()) if sib1 is None else dict(sib1)
    sib1["zero_corr_zone"] = args.zczc
    n_cs = n_cs_of(sib1)
    chips = zc_chips()
    y = msg1_tx(preamble, ta_true, n_cs, chips, args.prach_snr, args.seed)
    det = msg1_detect(y, chips, n_cs)
    n1 = dump_prach(args.prach_out, y, chips) if args.prach_out else 0
    rec, n2 = None, 0
    if args.msg_out:
        rec = rach_process(sib1, preamble, ta_true, occ,
                           prach_snr=args.prach_snr, pdcch_snr=args.pdcch_snr,
                           ms2_snr=args.ms2_snr, ms3_snr=args.ms3_snr,
                           ms4_snr=args.ms4_snr, seed=args.seed,
                           msg4_rv=args.msg4_rv, verbose=False)
        n2 = dump_msgs(args.msg_out, rec)
    return rec, det, n1, n2


def main_dump(argv):
    ap = argparse.ArgumentParser(description="RACH TD stimulus generator")
    ap.add_argument("--preamble", type=int, default=2)
    ap.add_argument("--ta", type=int, default=9)
    ap.add_argument("--occ", type=int, default=1)
    ap.add_argument("--zczc", type=int, default=0)
    ap.add_argument("--prach-snr", type=float, default=PRACH_SNR)
    ap.add_argument("--pdcch-snr", type=float, default=PDCCH_SNR)
    ap.add_argument("--ms2-snr", type=float, default=MS2_SNR)
    ap.add_argument("--ms3-snr", type=float, default=MS3_SNR)
    ap.add_argument("--ms4-snr", type=float, default=MS4_SNR)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--msg4-rv", type=int, default=None)
    ap.add_argument("--prach-out")
    ap.add_argument("--msg-out")
    a = ap.parse_args(argv)
    rec, det, n1, n2 = build_all(a.preamble, a.ta, a.occ, a)
    po, mo = prach_offsets(), msg_offsets()
    if a.prach_out:
        print("wrote %s (%d B: Msg1 %d w at 0, %d refs of %d taps from w %d)"
              % (a.prach_out, n1, 2 * L_RA, L_RA, PRACH_TAPS, po["refs"]))
    if a.msg_out:
        print("wrote %s (%d B: %d words)" % (a.msg_out, n2, mo["total"]))
        print("  msg word offsets: ms2_pdcch=%d ms2_pdsch=%d ms3_pusch=%d "
              "ms4_pdcch=%d ms4_pdsch=%d (total %d, sym %d, timed sym %d)" % (
                  mo["ms2_pdcch"], mo["ms2_pdsch"], mo["ms3_pusch"],
                  mo["ms4_pdcch"], mo["ms4_pdsch"], mo["total"], mo["sym_words"],
                  mo["timed_sym_words"]))
    line = "  Msg1 p=%d TA=%d (delay %d), margin %.1f dB" % (
        det["preamble"], det["ta"], det["delay"],
        10 * math.log10(max(1e-9, det["margin"])))
    if rec is not None:
        line += ", RA-RNTI=0x%04x TC-RNTI=0x%04x Msg3 window TA=%d" % (
            rec["ra_rnti"], rec["tc_rnti"], rec["ta_window"])
    print(line)
    if rec is None:
        return 0 if det["preamble"] == a.preamble and det["ta"] == a.ta else 1
    print("  Msg2 crc=%s Msg3 crc=%s Msg4 %s" % (
        rec["msg2_crc"], rec["msg3_crc"],
        "resolved" if rec["resolved"] else "NOT resolved"))
    return 0 if (rec["msg2_crc"] and rec["msg3_crc"] and rec["resolved"]) else 1


# ------------------------------------------------------------------ self test
def selftest():
    sib1 = dict(S1.default_sib1())
    sib1["zero_corr_zone"] = 0                 # N_CS = 15 -> 9 preambles/root
    n_cs = n_cs_of(sib1)
    ok = True
    print("4-step RACH golden model: L_RA=%d, N_CS=%d, %d preambles/root, "
          "taps=%d, chip scale=%d" % (L_RA, n_cs, preambles_per_root(n_cs),
                                      PRACH_TAPS, CHIP_SCALE))
    print("  from SIB1: prach_config_index=%d msg1_fdm=%d zero_corr_zone=%d"
          % (sib1["prach_config_index"], sib1["msg1_fdm"], sib1["zero_corr_zone"]))

    chips = zc_chips()
    for preamble, ta in ((0, 0), (2, 9), (5, 3), (8, 14)):
        for snr in (PRACH_SNR, 0.0):
            y = msg1_tx(preamble, ta, n_cs, chips, snr, preamble * 7 + ta + 1)
            d = msg1_detect(y, chips, n_cs)
            good = d["preamble"] == preamble and d["ta"] == ta
            ok &= good
            print("  Msg1 p=%d ta=%2d snr=%4.1f dB -> p=%d ta=%2d, margin %.1f dB %s"
                  % (preamble, ta, snr, d["preamble"], d["ta"],
                     10 * math.log10(max(1e-9, d["margin"])),
                     "OK" if good else "BAD"))

    for preamble, ta_true, occ, ms4 in ((2, 9, 1, MS4_SNR), (5, 3, 0, MS4_SNR),
                                        (2, 12, 3, 0.0)):
        rec = rach_process(sib1, preamble, ta_true, occ, ms4_snr=ms4,
                           seed=preamble + ta_true + occ, verbose=False)
        g = gates(rec)
        bad = [k for k, v in g.items() if not v]
        ok &= not bad
        print("  RA p=%d ta=%2d occ=%d(s%d,t%d,f%d) RA-RNTI=0x%04x Msg4 %4.1f dB: "
              "Msg1 %s Msg2 %s Msg3 %s, %d Msg4 tx, %s%s" % (
                  preamble, ta_true, occ, rec["occ_ids"][0], rec["occ_ids"][1],
                  rec["occ_ids"][2], rec["ra_rnti"], ms4,
                  "OK" if g["msg1_preamble"] and g["msg1_ta"] else "BAD",
                  "OK" if rec["msg2_crc"] else "BAD",
                  "OK" if rec["msg3_crc"] else "BAD", len(rec["msg4"]),
                  "resolved" if rec["resolved"] else "FAILED",
                  "" if not bad else "  BAD GATES: " + ",".join(bad)))

    base = rach_process(sib1, 2, 9, 1, seed=5, verbose=False)

    # TA coupling: the Msg3 window comes from the Msg1 estimate, and a window
    # pushed past the cyclic prefix reads the *next* symbol -> decode breaks.
    # (Inside the prefix a timing error is only a subcarrier phase rotation,
    # which the DMRS-based equaliser absorbs; that is real physics, not a gap.)
    cp_over = rach_process(sib1, 2, 9, 1, seed=5,
                           force_ta=base["msg1"]["ta"] + P.CP_LEN + 64,
                           verbose=False)
    ta_ok = base["msg3_crc"] and not cp_over["msg3_crc"] \
        and cp_over["ta_window"] == base["msg1"]["ta"] + P.CP_LEN + 64
    ok &= ta_ok
    print("  TA injection: window TA=%2d -> Msg3 %s ; window past the CP by 64 "
          "(TA=%4d) -> Msg3 %s : %s" % (
              base["ta_window"], "OK" if base["msg3_crc"] else "BAD",
              cp_over["ta_window"], "OK" if cp_over["msg3_crc"] else "BAD crc",
              "OK" if ta_ok else "BAD (the TA does not drive Msg3)"))

    # TC-RNTI coupling: Msg4 masked with another RNTI must not resolve.
    bad4 = rach_process(sib1, 2, 9, 1, seed=5, msg4_rnti_tx=0x1F1F, verbose=False)
    rnti_ok = base["resolved"] and not bad4["resolved"] and not bad4["msg4"][0]["dci_ok"]
    ok &= rnti_ok
    print("  TC-RNTI injection: 0x%04x resolves, 0x%04x %s : %s" % (
        base["tc_rnti"], bad4["rnti_tx"],
        "resolves" if bad4["resolved"] else "does not resolve",
        "OK" if rnti_ok else "BAD (RNTI does not gate Msg4)"))

    # Contention: a second UE took the same preamble, the gNB decoded *its* Msg3
    # and echoes that identity.  Msg4's CRC is fine, so only the CE comparison
    # can decide -- 38.321 5.1.5: unresolved, back off, restart from Msg1.
    other = pack_msg3_sdu(0x00BEEF01, 2)
    bad3 = rach_process(sib1, 2, 9, 1, seed=5, contend_with=other, verbose=False)
    last3 = bad3["msg4"][-1]
    cr_ok = (last3["crc_ok"] and last3["ce"] == other[:MAC.CR_ID_BITS // 8]
             and not last3["match"] and not bad3["resolved"]
             and bad3["c_rnti"] is None)
    ok &= cr_ok
    print("  contention: Msg3 %s, Msg4 %s but the CE carries the other UE "
          "(%s != %s) -> %s, backoff %s ms : %s" % (
              "OK crc" if bad3["msg3_crc"] else "BAD crc",
              "OK crc" if last3["crc_ok"] else "BAD crc",
              other[:6].hex(), bad3["sdu3"][:6].hex(),
              "resolved" if bad3["resolved"] else "NOT resolved",
              backoff_ms(RAR_BI),
              "OK" if cr_ok else "BAD (contention resolution is not load-bearing)"))

    print("[ %s ] 4-step random access (Msg1 PRACH -> Msg2 RAR -> Msg3 -> Msg4)"
          % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "dump":
        raise SystemExit(main_dump(sys.argv[2:]))
    raise SystemExit(selftest())
