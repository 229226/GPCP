#!/usr/bin/env python3
"""SIB1 golden model: Type0-PDCCH (SI-RNTI) + PDSCH carrying a compact SIB1.

This is Phase P1 of docs/11-项目计划.md: the UE has acquired PSS/SSS/PBCH/MIB
(already hardware-verified by run-ssb*), and now has to find **SIB1**.  SIB1 is
carried on DL-SCH (PDSCH) and scheduled by DCI format 1_0 whose CRC is
scrambled by the **SI-RNTI (0xFFFF)** in the Type0-PDCCH common search space
inside CORESET#0 (38.213 §13, 38.214 §5.1.3.1).

Transmit chain (gNB model):
    SIB1 config -> compact 80-bit SIB1 -> CRC24A -> LDPC BG2(Zc=20)
      -> rate match -> rate selection (rv -> k0) -> SI-RNTI scrambling
      -> 38.212 5.4.2.2 interleaving -> QPSK -> RE grid (Gold DMRS)
      -> flat gain + AWGN -> IFFT + CP -> int32 TD
    DCI 1_0 (mcs/rv/rb..., SI indicator in reserved[0]) -> Polar K=56
      -> CRC24C masked by SI-RNTI -> rate match E=432 -> QPSK -> 1-symbol TD

Receive chain (SoC model / future hardware):
    PDCCH 1 symbol -> FFT -> E=432 de-match -> Polar SC -> SI-RNTI CRC24C
      -> 32-bit DCI
    PDSCH 2 symbols -> FFT -> 7.4.1.1 Gold-DMRS LS -> equalise -> QPSK LLR
      -> de-interleave -> SI-RNTI descramble -> rate de-match
      -> 8 min-sum LDPC iterations -> CRC24A -> 80-bit SIB1 -> fields

Deliberate simplification (documented, same spirit as the project's 32-bit
DCI): the SIB1 payload is a **compact fixed-order 80-bit profile**, not a full
38.331 ASN.1 PER encoding.  It carries exactly the fields the next phase (P2
random access) needs: cell identity, TAC, PRACH configuration, SI scheduling
and CORESET#0.  Keeping it at 80 bits lets the whole verified PDSCH chain
(A_TB=80, BG2 Zc=20, E=720) and the existing run-pdsch/run-link-e2e hardware
be reused unchanged.

Constants below MUST stay in sync with sw/soc/link_e2e_bm.c (-DSIB1 variant).
"""
import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pdsch_plan as M
import pdsch_full_plan as F
import nr_pdcch as PDCCH
import ldpc5g as L

# ---- SIB1 compact profile (80 bits, MSB first) ----------------------------
# Field widths chosen so the total is exactly A_TB = 80 bits.
SIB1_FIELDS = [
    ("cell_identity", 36),        # 38.331 CellIdentity (36 bits)
    ("tac", 8),                   # tracking area code (compact: 8 of 24 bits)
    ("prach_config_index", 8),    # 38.211 6.3.3.2 prach-ConfigurationIndex
    ("msg1_fdm", 2),              # msg1-FDM (1/2/4/8)
    ("msg1_freq_start", 6),       # msg1-FrequencyStart (RB offset)
    ("zero_corr_zone", 4),        # zeroCorrelationZoneConfig
    ("preamble_target_power", 3), # preambleReceivedTargetPower index
    ("si_periodicity", 3),        # SI scheduling periodicity index
    ("si_window_len", 3),         # si-WindowLength index
    ("corset0_duration", 2),      # CORESET#0 duration (1..3 symbols)
    ("corset0_freq", 3),          # CORESET#0 frequency-domain resource
    ("scs", 2),                   # subCarrierSpacing (15/30/60/120 kHz)
]
SIB1_BITS = sum(w for _, w in SIB1_FIELDS)          # 80

SI_RNTI = 0xFFFF
SI_INDICATOR_BIT = 0            # reserved[0] = 1 -> SIB1 (not other SI)
SIB1_SNR = 20.0
PDCCH_SNR = 12.0
DEFAULT_SEED = 1

# --------------------------------------------------------------- rate de-match
def _dematch_rv(descr, rv):
    """Accumulate LLRs into the circular buffer at k0(rv), then expand to 1040.

    Uniform for every rv (rv=0 -> k0=0, identical to M.rate_dematch).
    """
    buf = [0] * F.USABLE
    k0 = F.RV_K0[rv]
    for k, v in enumerate(descr):
        buf[(k0 + k) % F.USABLE] += v
    dem = [0] * F.PUNCT
    u = 0
    for i in range(F.NCODED - F.PUNCT):
        if F.FILLER0 <= i < F.FILLER0 + F.FILLER_N:
            dem.append(0)
        else:
            dem.append(buf[u])
            u += 1
    return dem


# --------------------------------------------------------------- SIB1 bits
def pack_sib1(cfg):
    """Pack a SIB1 field dict into the compact 80-bit payload."""
    v = 0
    for name, w in SIB1_FIELDS:
        x = int(cfg.get(name, 0))
        if x < 0 or x >= (1 << w):
            raise ValueError("%s=%d does not fit in %d bits" % (name, x, w))
        v = (v << w) | x
    return [(v >> (SIB1_BITS - 1 - i)) & 1 for i in range(SIB1_BITS)]


def unpack_sib1(bits):
    """Inverse of pack_sib1."""
    if len(bits) != SIB1_BITS:
        raise ValueError("SIB1 must be %d bits, got %d" % (SIB1_BITS, len(bits)))
    v = 0
    for b in bits:
        v = (v << 1) | (b & 1)
    out = {}
    for name, w in reversed(SIB1_FIELDS):
        out[name] = v & ((1 << w) - 1)
        v >>= w
    return out


def default_sib1():
    """A realistic compact SIB1 profile (used by the benchmark)."""
    return dict(
        cell_identity=0x123456789,   # 36-bit CellIdentity
        tac=0x01,
        prach_config_index=159,      # 38.211 Table 6.3.3.2-2/3 (FR1, long)
        msg1_fdm=1,                  # 4 occasions
        msg1_freq_start=0,
        zero_corr_zone=12,
        preamble_target_power=4,
        si_periodicity=2,            # 160 ms
        si_window_len=2,
        corset0_duration=1,          # 2 symbols
        corset0_freq=3,
        scs=0,                       # 15 kHz
    )


# --------------------------------------------------------------- DCI 1_0
def sib1_dci_fields(cfg, rv=0):
    """DCI format 1_0 (SI-RNTI) mapped onto the project's 32-bit container."""
    return dict(mcs=cfg.get("mcs", 5), rv=rv, ndi=0, harq_pid=0,
                rb_start=0, rb_len=8, tpc=0,
                reserved=(1 << SI_INDICATOR_BIT))     # SI indicator = 1


# --------------------------------------------------------------- PDSCH TX/RX
def build_pdsch_grid_tb(tb, rv, rnti):
    """PDSCH coding chain for an explicit 80-bit transport block."""
    cw = L.encode(tb + M.crc24a(tb)
                  + [0] * (M.KB * M.ZC - M.A_TB - M.CRC_LEN), M.KB, M.ZC)
    _, usable, _filler = M.rate_match(cw)
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
    return grid, k0


def rx_pdsch_td(pdsch_td, rnti, rv=0):
    """Model of the SoC PDSCH receiver for an 80-bit DL-SCH block.

    Takes the *time-domain* 2-symbol blocks (the same thing the hardware loads
    at TD_RAM), strips CP and runs the fixed-point-style FFT via quantised_y.
    """
    yq = F.quantised_y(pdsch_td)
    h = F.channel_estimate_fid(yq)
    raw = M.extract_llr(yq, h)
    llr_e = F.deinterleave(raw)
    scr = M.scrambling_seq(rnti)
    descr = [-v if scr[k] else v for k, v in enumerate(llr_e)]
    dem = _dematch_rv(descr, rv)
    hard, scaled = F.hw_decode(dem)
    tb = hard[:M.A_TB]
    crc = hard[M.A_TB:M.A_TB + M.CRC_LEN]
    return dict(tb=tb, crc_ok=(M.crc24a(tb) == crc), hard=hard, h=h,
                raw=raw, dem=dem, scaled=scaled)


# --------------------------------------------------------------- TD packing
def _pack_td(blocks):
    out = bytearray()
    for blk in blocks:
        for c in blk:
            out += struct.pack("<I", (int(c.real) * F.TD_SCALE) & 0xFFFFFFFF)
            out += struct.pack("<I", (int(c.imag) * F.TD_SCALE) & 0xFFFFFFFF)
    return bytes(out)


def build_sib1(cfg=None, snr_db=SIB1_SNR, pdcch_snr=PDCCH_SNR,
               seed=DEFAULT_SEED, rv=0):
    """Build SIB1 end to end: returns (tb, dci_fields, pdcch_td, pdsch_td)."""
    cfg = default_sib1() if cfg is None else cfg
    tb = pack_sib1(cfg)
    dci = sib1_dci_fields(cfg, rv)
    syms, _ = PDCCH.transmit(dci, SI_RNTI)
    pdcch_td, _ = PDCCH.td_from_syms(syms, pdcch_snr, seed)
    grid, k0 = build_pdsch_grid_tb(tb, rv, SI_RNTI)
    y = M.add_channel(grid, snr_db, seed)
    pdsch_td = F.td_from_y(y)
    return dict(tb=tb, cfg=cfg, dci=dci, k0=k0,
                pdcch_td=pdcch_td, pdsch_td=pdsch_td, y=y)


def dump_td_bin(path, cfg=None, snr_db=SIB1_SNR, pdcch_snr=PDCCH_SNR,
                seed=DEFAULT_SEED, rv=0):
    """Write [ PDCCH TD (1 symbol) | PDSCH TD (2 symbols) ] for run-sib1."""
    r = build_sib1(cfg, snr_db, pdcch_snr, seed, rv)
    pd = _pack_td([r["pdcch_td"]])
    ps = _pack_td(r["pdsch_td"])
    with open(path, "wb") as f:
        f.write(pd)
        f.write(ps)
    return len(pd), len(ps)


# --------------------------------------------------------------- receive
def receive_sib1(pdcch_td, pdsch_td, rv=0):
    """Full SIB1 receive: PDCCH (SI-RNTI) -> DCI -> PDSCH -> SIB1 fields."""
    q = PDCCH.quantised_syms(pdcch_td)
    det = PDCCH.receive(q, [SI_RNTI], SI_RNTI)
    dci_ok = det["ok"] and det["rnti"] == SI_RNTI and det["fields"] is not None
    dci = det["fields"] or {}
    res = rx_pdsch_td(pdsch_td, SI_RNTI, rv)
    sib1 = unpack_sib1(res["tb"]) if res["crc_ok"] else None
    return dict(dci_ok=dci_ok, dci=dci, pdcch_crc=det["ok"],
                pdsch_crc=res["crc_ok"], tb=res["tb"], sib1=sib1,
                hard=res["hard"], h=res["h"])


# --------------------------------------------------------------- self test
def selftest():
    cfg = default_sib1()
    ok = True
    print("SIB1 golden model self-test (compact %d-bit profile, SI-RNTI=0x%04x)"
          % (SIB1_BITS, SI_RNTI))
    print("  fields: %s" % ", ".join("%s(%d)" % (n, w) for n, w in SIB1_FIELDS))

    # bit round trip
    bits = pack_sib1(cfg)
    assert len(bits) == M.A_TB, "SIB1 must be exactly %d bits" % M.A_TB
    assert unpack_sib1(bits) == cfg
    print("  bit round-trip: %d bits OK" % len(bits))

    for rv in (0, 2):
        for snr in (20.0, 6.0):
            r = build_sib1(cfg, snr_db=snr, seed=1, rv=rv)
            rx = receive_sib1(r["pdcch_td"], r["pdsch_td"], rv)
            dci_match = rx["dci"] == r["dci"]
            sib1_match = rx["sib1"] == cfg
            tb_match = rx["tb"] == r["tb"]
            good = (rx["dci_ok"] and dci_match and rx["pdsch_crc"]
                    and tb_match and sib1_match)
            ok &= good
            print("  rv=%d snr=%4.1f dB : pdcch=%s dci=%s pdsch_crc=%s "
                  "tb=%s sib1=%s%s" % (
                      rv, snr, "OK" if rx["pdcch_crc"] else "BAD",
                      "OK" if dci_match else "BAD",
                      "OK" if rx["pdsch_crc"] else "BAD",
                      "OK" if tb_match else "BAD",
                      "OK" if sib1_match else "BAD",
                      "  [OK]" if good else "  [FAIL]"))

    # decode the key P2-facing fields explicitly
    r = build_sib1(cfg, seed=1, rv=0)
    rx = receive_sib1(r["pdcch_td"], r["pdsch_td"], 0)
    print("  decoded SIB1: cell_identity=0x%09x tac=%d prach_config_index=%d "
          "msg1_fdm=%d scs=%d" % (
              rx["sib1"]["cell_identity"], rx["sib1"]["tac"],
              rx["sib1"]["prach_config_index"], rx["sib1"]["msg1_fdm"],
              rx["sib1"]["scs"]))
    print("[ %s ] SIB1 model (Type0-PDCCH SI-RNTI -> PDSCH -> SIB1 fields)"
          % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="SIB1 golden model / TD generator")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("selftest")
    d = sub.add_parser("dump")
    d.add_argument("out")
    d.add_argument("--snr", type=float, default=SIB1_SNR)
    d.add_argument("--pdcch-snr", type=float, default=PDCCH_SNR)
    d.add_argument("--seed", type=int, default=DEFAULT_SEED)
    d.add_argument("--rv", type=int, default=0)
    a = ap.parse_args()

    if a.cmd == "dump":
        n1, n2 = dump_td_bin(a.out, None, a.snr, a.pdcch_snr, a.seed, a.rv)
        print("wrote %s (PDCCH %d B + PDSCH %d B, rv=%d, snr=%.1f dB, "
              "seed=%d, SI-RNTI=0x%04x)" % (a.out, n1, n2, a.rv, a.snr,
                                            a.seed, SI_RNTI))
        return 0
    return selftest()


if __name__ == "__main__":
    sys.exit(main())
