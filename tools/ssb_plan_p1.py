#!/usr/bin/env python3
"""Real (38.211 / 38.212) SSB golden model + reference receiver -- P1 fidelity.

This is the protocol-faithful counterpart of tools/ssb_plan.py (the simplified
demo model).  Differences vs the demo model:

  * PSS in OFDM symbol 0, SSS in symbol 2            (real NR; demo used 1 / 3)
  * PBCH DMRS c_init uses 38.211 7.4.1.3            (i_SSB / n_hf), not N_ID^cell
  * carrier subcarrier mapping carries a DC null + k_ssb offset (38.211 7.4.3.1)
  * a cyclic prefix is inserted in the time domain
  * PBCH is rate matched to E bits via 38.212 5.4.1
    (sub-block interleave -> circular buffer -> triangular interleave)

The receiver mirrors, step by step, what the C benchmark (sw/soc/ssb_fid_bm.c)
will do, so a passing Python self-test is the gate before touching silicon.
"""
import nr_seq
import nr_pbch as P
import math
import random
import sys
import struct
from ssb_plan import (bitrev, is_frozen, info_indices, polar_encode, sc_decode,
                      Lcg, wrap32, f_node, g_node, _fft)

# re-export the 38.211 7.4.1.3 DMRS init so the checker (tools/check_ssb_fid.py)
# can compare it without reaching into nr_pbch
pbch_dmrs_cinit = P.pbch_dmrs_cinit

# ---- layout / scalars (must match sw/soc/ssb_fid_bm.c) -------------------
NSYM = 4
NRE = 240
PSS_RE0 = 56
NPSS = 127
DMRS_STEP = 4
NPILOT = NRE // DMRS_STEP          # 60
KINFO = 32
NPOL = 512
DEPTH = 9
QPSK = 2000
CH_RE = 2
CH_IM = 1
LLR_SHIFT = 2
H_SCALE = 16
NOISE = 512
NFFT = 4096
TD_SCALE = 256
DC_RE = 24                         # carrier DC lands exactly on bin NFFT//2 (2048), and
                                  # r=24 is a DMRS-grid position (step 4) so nulling it
                                  # costs a pilot, not a PBCH data RE -> still 180 data
                                  # REs/symbol -> 720 PBCH bits == E.
CP_LEN = 288                       # illustrative normal-CP length
PBCH_SYMS = [1, 3]
E = 2 * (NRE - NPILOT) * len(PBCH_SYMS)   # 720 rate-matched bits

TRUE_NID1 = 200
TRUE_NID2 = 1
I_SSB = 3
N_HF = 0


# --------------------------------------------------------------- sequences
def pss_seq(nid2):
    return nr_seq.pss_seq(nid2)


def sss_seq(nid1, nid2):
    return nr_seq.sss_seq(nid1, nid2)


def dmrs_values(c_init):
    c = P.gold(c_init, 2 * len(PBCH_SYMS) * NPILOT)
    return [complex(1 - 2 * c[2 * n], 1 - 2 * c[2 * n + 1]) * QPSK
            for n in range(len(PBCH_SYMS) * NPILOT)]


def bin_of(r, k_ssb=0):
    """Carrier FFT bin for SSB RE r (DC null at bin NFFT//2)."""
    return NFFT // 2 - DC_RE + k_ssb + r


def pbch_data_list():
    """Ordered (sym, re) of PBCH data REs -- identical TX/RX scan order."""
    out = []
    for s in PBCH_SYMS:
        for r in range(NRE):
            if r % DMRS_STEP == 0:
                continue
            out.append((s, r))
    return out


def payload_bits(seed=0x1234):
    g = Lcg(seed)
    return [(g.step() >> 31) & 1 for _ in range(24)] + [0] * 8


def nid_cell(nid1, nid2):
    return P.nid_cell(nid1, nid2)


# --------------------------------------------------------------- transmitter
def build_grid(nid1=TRUE_NID1, nid2=TRUE_NID2, i_ssb=I_SSB, n_hf=N_HF, payload=None):
    if payload is None:
        payload = payload_bits()
    ufull = [0] * NPOL
    for k, i in enumerate(info_indices()):
        ufull[i] = payload[k]
    d = polar_encode(ufull)
    e = P.rate_match_pbch(d, E)                       # 720 rate-matched bits
    scr = P.gold(nid_cell(nid1, nid2), E)             # 38.211 7.3.2: c_init = N_ID^cell
    e_sc = [e[b] ^ scr[b] for b in range(E)]

    grid = [[0j] * NRE for _ in range(NSYM)]
    pss = pss_seq(nid2)
    sss = sss_seq(nid1, nid2)
    ci_dmrs = P.pbch_dmrs_cinit(nid1, nid2, i_ssb, n_hf)
    dr = dmrs_values(ci_dmrs)

    for k, r in enumerate(range(PSS_RE0, PSS_RE0 + NPSS)):
        grid[0][r] = complex(pss[k], 0) * QPSK
        grid[2][r] = complex(sss[k], 0) * QPSK
    p = 0
    for s in PBCH_SYMS:
        for r in range(0, NRE, DMRS_STEP):
            if r == DC_RE:
                continue
            grid[s][r] = dr[p]
            p += 1
    dl = pbch_data_list()
    for m in range(len(dl)):
        s, r = dl[m]
        b0 = e_sc[2 * m]
        b1 = e_sc[2 * m + 1]
        grid[s][r] = complex(1 - 2 * b0, 1 - 2 * b1) * QPSK
    return grid, payload, e_sc


def build_td(nid1=TRUE_NID1, nid2=TRUE_NID2, i_ssb=I_SSB, n_hf=N_HF, payload=None):
    grid, payload, e_sc = build_grid(nid1, nid2, i_ssb, n_hf, payload)
    td = []
    for s in range(NSYM):
        freq = [0j] * NFFT
        for r in range(NRE):
            if r == DC_RE:
                continue
            freq[bin_of(r)] = grid[s][r]
        sig = _fft(freq, inverse=True)
        samples = [int(round(c.real)) + 1j * int(round(c.imag)) for c in sig]
        samples = P.add_cp(samples, CP_LEN)
        td.append(samples)
    return td, grid, payload, e_sc


def dump_td(path, nid1=TRUE_NID1, nid2=TRUE_NID2, payload=None,
            snr_db=None, lcg_seed=0x50B0A5):
    if snr_db is None:
        td, grid, payload, e_sc = build_td(nid1, nid2, payload=payload)
    else:
        td, grid, payload, e_sc = build_td_noisy(nid1, nid2, I_SSB, N_HF,
                                                 payload, snr_db, lcg_seed)
    with open(path, "w") as f:
        for s in range(NSYM):
            for c in td[s]:
                f.write(f"{int(c.real) * TD_SCALE} {int(c.imag) * TD_SCALE}\n")
    return payload, e_sc


def dump_td_bin(path, nid1=TRUE_NID1, nid2=TRUE_NID2, payload=None, snr_db=None, lcg_seed=0x50B0A5):
    """Binary int32 TD dump for the Verilator SoC (matches tools/ssb_plan.py's
    _dump_bin); each symbol is SYM_LEN = CP_LEN + NFFT complex samples."""
    td, grid, payload, e_sc = build_td(nid1, nid2, payload=payload)
    sym_len = CP_LEN + NFFT
    with open(path, "wb") as f:
        for s in range(NSYM):
            # pad the useful symbol (length NFFT) into a CP_LEN+NFFT window
            for k in range(sym_len):
                c = td[s][k]
                f.write(struct.pack("<I", (int(c.real) * TD_SCALE) & 0xFFFFFFFF))
                f.write(struct.pack("<I", (int(c.imag) * TD_SCALE) & 0xFFFFFFFF))
    return payload, e_sc


def _freq_of_symbol(grid, s):
    """Bin the (sym,re) grid into an NFFT-length frequency array (DC null)."""
    freq = [0j] * NFFT
    for r in range(NRE):
        if r == DC_RE:
            continue
        freq[bin_of(r)] = grid[s][r]
    return freq


def build_td_noisy(nid1=TRUE_NID1, nid2=TRUE_NID2, i_ssb=I_SSB, n_hf=N_HF,
                   payload=None, snr_db=25.0, lcg_seed=0x50B0A5):
    """Build the time-domain SSB with a flat channel + per-subcarrier AWGN.

    SNR is defined per active subcarrier:  E[|h*X|^2] / N0 = snr_db.
    The AWGN is added in the frequency domain (the standard OFDM model --
    statistically identical to time-domain AWGN under the unitary FFT) and
    then IFFT'd back to time, so the C benchmark that re-FFT's the samples
    sees exactly the same subcarrier SNR.  This avoids the ~19 dB offset a
    naive time-domain noise scaling introduces through the non-unitary FFT.
    """
    grid, payload, e_sc = build_grid(nid1, nid2, i_ssb, n_hf, payload)
    h = complex(CH_RE, CH_IM)
    hs = abs(h) ** 2
    rnd = random.Random(lcg_seed)
    td = []
    for s2 in range(NSYM):
        freq = _freq_of_symbol(grid, s2)
        sp = 0.0
        nact = 0
        for k in range(NFFT):
            if freq[k] != 0j:
                sp += abs(freq[k]) ** 2
                nact += 1
        if nact:
            sp /= nact
        n0 = hs * sp / (10.0 ** (snr_db / 10.0))        # complex noise variance
        sd = math.sqrt(n0 / 2.0)
        for k in range(NFFT):
            if freq[k] != 0j:
                freq[k] = freq[k] * h
                freq[k] += complex(rnd.gauss(0.0, sd), rnd.gauss(0.0, sd))
        sig = _fft(freq, inverse=True)                  # normalized IFFT
        samples = [int(round(c.real)) + 1j * int(round(c.imag)) for c in sig]
        td.append(P.add_cp(samples, CP_LEN))
    return td, grid, payload, e_sc

# --------------------------------------------------------------- receiver
def receiver(y, nid1, nid2, i_ssb=I_SSB, n_hf=N_HF):
    spec = []
    for s in range(NSYM):
        useful = y[s][CP_LEN:CP_LEN + NFFT]
        spec.append(_fft(useful, inverse=False))
    yw = [[0j] * NRE for _ in range(NSYM)]
    for s in range(NSYM):
        for r in range(NRE):
            if r == DC_RE:
                continue
            yw[s][r] = spec[s][bin_of(r)]

    ci_dmrs = P.pbch_dmrs_cinit(nid1, nid2, i_ssb, n_hf)
    dr = dmrs_values(ci_dmrs)
    h = []
    den = NPILOT * 2 * QPSK * QPSK // H_SCALE
    p = 0
    for s in PBCH_SYMS:
        acc = 0j
        for r in range(0, NRE, DMRS_STEP):
            if r == DC_RE:
                continue
            d = dr[p]
            p += 1
            acc += yw[s][r] * complex(d.real, -d.imag)
        h.append(complex(int(acc.real // den), int(acc.imag // den)))

    scr = P.gold(nid_cell(nid1, nid2), E)
    llr = [0] * E
    dl = pbch_data_list()
    for m in range(len(dl)):
        s, r = dl[m]
        hs = h[PBCH_SYMS.index(s)]
        eq = yw[s][r] * complex(hs.real, -hs.imag)
        for bit, soft in ((0, eq.real), (1, eq.imag)):
            v = int(soft) >> LLR_SHIFT
            v = max(-2000000, min(2000000, v))
            llr[2 * m + bit] = -v if scr[2 * m + bit] else v

    z = P.rate_recover_pbch(llr, NPOL)              # 512 LLRs
    bits = sc_decode(z)
    return [bits[i] for i in info_indices()]


def run_one(snr_db=25.0, seed=0x50B0A5):
    payload = payload_bits(seed)
    td, _, _, _ = build_td(payload=payload)
    td, _, _, _ = build_td_noisy(payload=payload, snr_db=snr_db, lcg_seed=seed)
    y = td
    rx = receiver(y, TRUE_NID1, TRUE_NID2)
    return rx, payload


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="selftest",
                   help="dump | selftest (default selftest)")
    ap.add_argument("out", nargs="?", default="build/soc/ssb_fid.td")
    ap.add_argument("--snr", type=float, default=None,
                   help="if set, dump a noisy TD at this SNR (dB)")
    ap.add_argument("--seed", type=int, default=0x50B0A5)
    args = ap.parse_args()
    if args.cmd == "dump":
        payload, e_sc = dump_td_bin(args.out, snr_db=args.snr, lcg_seed=args.seed)
        print("wrote", args.out, "snr_db", args.snr, "payload", payload)
        sys.exit(0)
    rx, payload = run_one(25.0)
    mism = sum(1 for a, b in zip(rx, payload) if a != b)
    print("MIB mismatches @25dB:", mism, "/", len(payload))
    assert mism == 0, "high-SNR decode failed"
    for snr in (-5, 0, 4, 8, 12, 16, 20, 25):
        e = t = 0
        for _ in range(10):
            rx, pl = run_one(snr_db=snr)
            e += sum(1 for a, b in zip(rx, pl) if a != b)
            t += len(pl)
        print(f"SNR={snr:>3}dB  BER={e/t:.4f}")
    print("OK ssb_plan_p1 self-test (MIB 32/32, real 38.211/38.212 chain)")
