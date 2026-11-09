#!/usr/bin/env python3
"""G1/A3 golden model: OFDM transmit waveform (IFFT + CP) on the existing FFT engine.

Why no new microcode: the hardware FFT is a Q15 radix-4 DIF transform with a
1/4 scaling per stage, so for N=4096 (six stages) its output is DFT(a)/N in
base-4 digit-reversed order.  Therefore

    IDFT(X)[n] = conj( FFT_hw(conj(X))[rev4(n)] )

and the conjugate trick is pure data preparation on CVA6.  The two generated
images fft_stage_prog.h / fft_block_prog.h are reused unchanged.

Level convention (already proven by the receive chain, see ssb_plan_p1.py):
  * frequency grid holds QPSK symbols of amplitude 2000;
  * time samples from IDFT are multiplied by TD_SCALE=256 before the RX FFT;
  * the RX FFT divides by 4096, so the recovered bin is X/16 and `<<4`
    restores X.  selftest-ifft simulates this loop and checks the hard decisions.

CLI:
    python3 tools/nr_ofdm_tx.py selftest-ifft
    python3 tools/nr_ofdm_tx.py dump-ifft --out build/ofdm/ifft.td [--sym 1]
"""
import argparse
import math
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fft_plan as F                                    # noqa: E402
import ssb_plan_p1 as P1                                # noqa: E402
import ssb_plan as S                                    # noqa: E402

NFFT = P1.NFFT                # 4096
CP_LEN = P1.CP_LEN            # 288
TD_SCALE = P1.TD_SCALE        # 256
QPSK = P1.QPSK                # 2000
MAGIC = 0x1FF70001        # A3: one conjugate spectrum
MAGIC4 = 0x1FF70004       # A4: four conjugate spectra (full SSB)
HDR_WORDS = 8
NSYM = P1.NSYM                 # 4 OFDM symbols


def symbol_grid(sym=1):
    """Real 38.211 SSB grid for one OFDM symbol -> 4096-bin frequency array."""
    grid, payload, e_sc = P1.build_grid()
    freq = [0j] * NFFT
    for r in range(P1.NRE):
        if r == P1.DC_RE:
            continue
        freq[P1.bin_of(r)] = grid[sym][r]
    return freq


def conj_input(freq):
    """Frequency-domain conjugate, int16 (re, im), natural bin order."""
    return [(int(round(c.real)), -int(round(c.imag))) for c in freq]


def read_conj_td(path, magic=MAGIC):
    """Read A3/A4 conjugate TD; header[4] is the number of OFDM symbols."""
    with open(path, "rb") as f:
        hdr = struct.unpack("<%dI" % HDR_WORDS, f.read(4 * HDR_WORDS))
        assert hdr[0] == magic, f"bad TD magic 0x{hdr[0]:08x}"
        assert hdr[1] == NFFT and hdr[2] == CP_LEN and hdr[3] == TD_SCALE
        nsym = hdr[4]
        assert 1 <= nsym <= NSYM, f"bad symbol count {nsym}"
        raw = f.read()
    vals = struct.unpack("<%di" % (len(raw) // 4), raw)
    assert len(vals) == 2 * NFFT * nsym
    return list(vals[0::2]), list(vals[1::2]), hdr


def hw_ifft_raw(re, im):
    """Bit-exact reference for the hardware FFT (run-fft proves equivalence)."""
    return F.planned_fft(list(re), list(im), NFFT)


def raw_to_td(raw_re, raw_im, cp=CP_LEN, scale=TD_SCALE):
    """CVA6 post-processing: conjugate, rev4 reorder, scale, cyclic prefix.

    FFT_hw output position i holds DFT(a)[rev4(i)]; rev4 is an involution, so
    the sample at time n is conj(raw[rev4(n)]).
    """
    rev = F.digit_reversed(NFFT)
    x = [None] * NFFT
    for i in range(NFFT):
        x[rev[i]] = (raw_re[i], -raw_im[i])
    y = [(r * scale, i * scale) for r, i in x]
    return y[NFFT - cp:] + y


def dump_ifft(path, sym=1):
    freq = symbol_grid(sym)
    ci = conj_input(freq)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        for v in (MAGIC, NFFT, CP_LEN, TD_SCALE, sym, QPSK, 0, 0):
            f.write(struct.pack("<I", v))
        for r, i in ci:
            f.write(struct.pack("<ii", r, i))
    return freq, ci


def dump_ssb_tx(path):
    """A4: four conjugate spectra, one per SSB OFDM symbol (P1 grid)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    all_ci = []
    with open(path, "wb") as f:
        for v in (MAGIC4, NFFT, CP_LEN, TD_SCALE, NSYM, QPSK, 0, 0):
            f.write(struct.pack("<I", v))
        for s in range(NSYM):
            ci = conj_input(symbol_grid(s))
            for r, i in ci:
                f.write(struct.pack("<ii", r, i))
            all_ci.append(ci)
    return all_ci


def ssb_tx_expected(re, im, nsym=NSYM):
    """Expected A4 TX samples: nsym blocks of (CP+NFFT) complex pairs."""
    out = []
    for s in range(nsym):
        sl = slice(s * NFFT, (s + 1) * NFFT)
        rr, ri = hw_ifft_raw(re[sl], im[sl])
        out.extend(raw_to_td(rr, ri))
    return out


def loopback_sign_errors(freq, td):
    """Feed the TX samples back through the RX FFT and count QPSK sign errors."""
    yre = [v[0] for v in td[CP_LEN:]]
    yim = [v[1] for v in td[CP_LEN:]]
    rr, ri = F.planned_fft(yre, yim, NFFT)
    rev = F.digit_reversed(NFFT)
    got = [0j] * NFFT
    for i in range(NFFT):
        got[rev[i]] = complex(rr[i] << 4, ri[i] << 4)
    active = [k for k, c in enumerate(freq) if c != 0]
    bad = 0
    for k in active:
        if (got[k].real >= 0) != (freq[k].real >= 0):
            bad += 1
            continue
        # PSS/SSS are BPSK (imag == 0); a noisy zero imaginary part must not
        # be treated as a sign error.  QPSK data/DMRS REs carry both bits.
        if freq[k].imag != 0 and (got[k].imag >= 0) != (freq[k].imag >= 0):
            bad += 1
    return bad, len(active)


def selftest_ssb_tx():
    bads = []
    for s in range(NSYM):
        freq = symbol_grid(s)
        ci = conj_input(freq)
        rr, ri = hw_ifft_raw([v[0] for v in ci], [v[1] for v in ci])
        td = raw_to_td(rr, ri)
        bad, active = loopback_sign_errors(freq, td)
        bads.append(bad)
        print(f"[info] A4 symbol {s}: {active} active REs, "
              f"{bad} QPSK sign errors in the loopback simulation")
    if any(bads):
        print("[FAIL] 4-symbol level convention broken")
        return 1
    print("[ OK ] A4 model: all four SSB symbols round-trip through the "
          "receive FFT with 0 QPSK sign errors")
    return 0


def selftest(sym=1):
    freq = symbol_grid(sym)
    ci = conj_input(freq)
    re = [v[0] for v in ci]
    im = [v[1] for v in ci]
    assert max(max(abs(v) for v in re), max(abs(v) for v in im)) < 32768, \
        "conjugate input must fit int16"

    raw_re, raw_im = hw_ifft_raw(re, im)
    td = raw_to_td(raw_re, raw_im)

    # independent floating-point reference (numpy-free radix-2 IFFT, 1/N)
    ref = S._fft(freq, inverse=True)
    rms = math.sqrt(sum((td[CP_LEN + n][0] / TD_SCALE - ref[n].real) ** 2 +
                        (td[CP_LEN + n][1] / TD_SCALE - ref[n].imag) ** 2
                        for n in range(NFFT)) / NFFT)
    mag = math.sqrt(sum(abs(c) ** 2 for c in ref) / NFFT)
    snr = 20 * math.log10(mag / max(rms, 1e-9))

    # level-convention loop: feed the TX samples back into the RX FFT and <<4
    yre = [v[0] for v in td[CP_LEN:]]
    yim = [v[1] for v in td[CP_LEN:]]
    rr, ri = F.planned_fft(yre, yim, NFFT)
    rev = F.digit_reversed(NFFT)
    got = [0j] * NFFT
    for i in range(NFFT):
        got[rev[i]] = complex(rr[i] << 4, ri[i] << 4)
    active = [k for k, c in enumerate(freq) if c != 0]
    bad = sum(1 for k in active
              if (got[k].real >= 0) != (freq[k].real >= 0) or
                 (got[k].imag >= 0) != (freq[k].imag >= 0))
    err = math.sqrt(sum(abs(got[k] - freq[k]) ** 2 for k in active) / len(active))
    print(f"[info] A3 model: symbol {sym}, {len(active)} active REs, "
          f"TD rms {mag:.2f} / peak "
          f"{max(max(abs(v[0]), abs(v[1])) for v in td) // TD_SCALE}, "
          f"fixed-point SNR {snr:.1f} dB")
    print(f"[info] A4 loopback simulation: {bad} QPSK sign errors, "
          f"amplitude rms error {err:.1f} (constellation {QPSK})")
    if bad:
        print("[FAIL] level convention broken: transmitter samples do not "
              "round-trip through the receiver FFT")
        return 1
    if rms > 2.0:
        print(f"[FAIL] fixed-point IFFT deviates from float reference: rms {rms:.2f}")
        return 1
    print("[ OK ] A3 model: conjugate IFFT + TD_SCALE + CP round-trips through "
          "the receive chain with 0 sign errors")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("selftest-ifft", "dump-ifft",
                                    "selftest-ssb-tx", "dump-ssb-tx"))
    ap.add_argument("--out")
    ap.add_argument("--sym", type=int, default=1, choices=range(P1.NSYM))
    a = ap.parse_args()
    if a.cmd == "selftest-ifft":
        return selftest(a.sym)
    if a.cmd == "selftest-ssb-tx":
        return selftest_ssb_tx()
    if not a.out:
        ap.error("dump needs --out")
    if a.cmd == "dump-ssb-tx":
        dump_ssb_tx(a.out)
        print(f"wrote {a.out}: {NSYM} x {NFFT} conjugate bins "
              f"({2 * NFFT * NSYM} int32 words)")
        return 0
    freq, ci = dump_ifft(a.out, a.sym)
    print(f"wrote {a.out}: {NFFT} conjugate bins ({2 * NFFT} int32 words), "
          f"{sum(1 for c in freq if c != 0)} active REs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
