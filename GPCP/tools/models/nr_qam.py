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
"""Higher-order QAM for the PDSCH data plane (P4 of docs/project/README.md).

38.211 7.3.1.2 square QAM with Gray labelling, in the same integer fixed-point
style as the existing project QPSK engine (tools/pdsch_plan.py):

    QPSK  (Qm=2)  amplitude 2000, levels +/-1                (normaliser 1)
    16QAM (Qm=4)  levels +/-1, +/-3 with a = 894  (= 2000/sqrt(5))
    64QAM (Qm=6)  levels +/-1, +/-3, +/-5, +/-7 with a = 436 (= 2000/sqrt(21))

The normalisation keeps the *average* symbol power equal across all three
orders, so M.add_channel() (which derives noise power from the grid) gives the
same SNR definition for every MCS and link adaptation stays meaningful.

Bit order per symbol (MSB first), matching tools/pdsch_plan.qpsk_map's rule
"bit 0 -> +": for the I branch with n = Qm/2 bits,

    I = (1 - 2*b0) * (2^(n-1) - 1 - 2*gray(b1..)) * a

i.e. b0 is the sign and the remaining bits select the magnitude in Gray order:
    16QAM  b0b1 : 00 -> +3, 01 -> +1, 10 -> -3, 11 -> -1
    64QAM  b0b1b2: 000 -> +7, 001 -> +5, 011 -> +3, 010 -> +1,
                    100 -> -7, 101 -> -5, 111 -> -3, 110 -> -1

Soft demodulation is max-log on the matched-filtered sample eq = y*conj(h):
for each bit, LLR = min(dist over bit=1) - min(dist over bit=0), where the
distance is the squared distance to the reference levels scaled by |h|^2*a.
The integer division by |h|^2*a brings the LLR back to the same linear scale
as the project's QPSK LLR (eq.real / eq.imag), so the existing >>8 LLR_SHIFT
and SENTINEL=32767 headroom still apply.

P4-C implements exactly these integer operations on the SoC.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

QPSK_AMP = 2000
H_SCALE = 16                                   # LS channel-estimate scaling
# Integer level scales: keep average symbol energy equal across orders.
LEVEL_SCALE = {2: QPSK_AMP, 4: 894, 6: 436}     # 2000/sqrt(5), 2000/sqrt(21)
QM_OF_ORDER = {2: 1, 4: 2, 6: 3}                # bits per I/Q branch
# Magnitude (in units of LEVEL_SCALE) indexed by the Gray magnitude bits.
MAGNITUDE = {
    1: {(): 1},                                  # QPSK: sign bit only
    2: {(0,): 3, (1,): 1},
    3: {(0, 0): 1, (0, 1): 3, (1, 1): 5, (1, 0): 7},
}


def order_of(qm):
    if qm not in QM_OF_ORDER:
        raise ValueError("unsupported Qm %r (use 2, 4 or 6)" % qm)
    return QM_OF_ORDER[qm]


def levels(qm):
    """Positive magnitudes in Gray-bit order, MSB-first bit patterns."""
    n = order_of(qm)
    if n == 1:
        return [1]
    if n == 2:
        return [3, 1]                 # b1 = 0 -> 3, b1 = 1 -> 1
    return [7, 5, 3, 1]              # b1b2 = 00,01,11,10 in Gray code


def branch_bits(qm):
    """All bit patterns for one I/Q branch, in the same order as levels()*signs."""
    n = order_of(qm)
    if n == 1:
        return [[0], [1]]
    if n == 2:
        return [[0, 0], [0, 1], [1, 1], [1, 0]]
    return [[0, 0, 0], [0, 0, 1], [0, 1, 1], [0, 1, 0],
            [1, 0, 0], [1, 0, 1], [1, 1, 1], [1, 1, 0]]


def constellation(qm):
    """[(bits_tuple, level)] for one branch, level signed and unscaled.

    b0 is the sign bit (0 -> +), the remaining bits are a Gray-coded
    magnitude.  Decoding the magnitude from the bits themselves (rather than
    from the pattern's position in a sorted list) is what keeps adjacent
    levels one bit apart across the origin.
    """
    n = order_of(qm)
    out = []
    for pat in branch_bits(qm):
        sign = 1 if pat[0] == 0 else -1
        mag = MAGNITUDE[n][tuple(pat[1:])]
        out.append((tuple(pat), sign * mag))
    return out


def qam_map(bits, qm):
    """Map a bit vector to complex symbols (integer re/im)."""
    if len(bits) % qm:
        raise ValueError("bit length %d not a multiple of Qm %d" % (len(bits), qm))
    a = LEVEL_SCALE[qm]
    n = order_of(qm)
    out = []
    for i in range(0, len(bits), qm):
        grp = list(bits[i:i + qm])
        ib, qb = grp[:n], grp[n:]
        i_lvl = _branch_level(ib, qm)
        q_lvl = _branch_level(qb, qm)
        out.append(complex(i_lvl * a, q_lvl * a))
    return out


def _branch_level(bits, qm):
    for pat, lvl in constellation(qm):
        if list(pat) == list(bits):
            return lvl
    raise ValueError("invalid branch bits %s for Qm %d" % (bits, qm))


def qam_hard(sym, h, qm):
    """Hard bit decisions from a matched-filtered QAM symbol (debug helper)."""
    return [1 if v < 0 else 0 for v in qam_llr(sym, h, qm)]


def qam_llr(sym, h, qm):
    """Max-log LLRs for one received symbol, in the project's integer scale.

    ``h`` is the per-symbol LS channel estimate (complex, H_SCALE-scaled).
    Returns Qm LLRs, MSB first (I branch then Q branch); positive -> bit 0.
    """
    a = LEVEL_SCALE[qm]
    hr, hi = int(h.real), int(h.imag)
    h2 = hr * hr + hi * hi
    if h2 <= 0:
        return [0] * qm
    # The LS estimate is H_SCALE times the true channel, so |h|^2/H_SCALE is
    # the effective gain of the matched filter y*conj(h).  Dividing the squared
    # distances by 2*ref turns the max-log metric back into a *linear* LLR of
    # the same magnitude as the project's QPSK LLR (eq.real), which keeps the
    # existing >>8 LLR_SHIFT / SENTINEL=32767 headroom valid.
    ref = (h2 // H_SCALE) * a
    if ref <= 0:
        ref = a
    eq = sym * complex(hr, -hi)
    return (_branch_llr(int(eq.real), ref, qm)
            + _branch_llr(int(eq.imag), ref, qm))


def _branch_llr(u, ref, qm):
    """LLRs for the n = Qm/2 bits of one (I or Q) branch."""
    n = order_of(qm)
    cons = constellation(qm)
    out = []
    for k in range(n):
        d1 = min((u - ref * lvl) ** 2 for pat, lvl in cons if pat[k] == 1)
        d0 = min((u - ref * lvl) ** 2 for pat, lvl in cons if pat[k] == 0)
        # Sign convention: positive LLR -> bit 0 more likely (same as QPSK).
        # Truncating division (C semantics), not Python's floor division.
        num = d1 - d0
        q = abs(num) // (2 * ref)
        out.append(-q if num < 0 else q)
    return out


def bits_per_symbol(qm):
    return qm


def symbols_for_bits(nbits, qm):
    if nbits % qm:
        raise ValueError("bit length %d not a multiple of Qm %d" % (nbits, qm))
    return nbits // qm


# ---------------------------------------------------------------- self test
def selftest():
    import random
    ok = True

    def chk(cond, msg):
        nonlocal ok
        if not cond:
            print("  " + msg)
            ok = False

    print("Higher-order QAM (P4): 38.211 7.3.1.2 Gray mapping + integer max-log LLR")
    rnd = random.Random(11)

    for qm in (2, 4, 6):
        n = order_of(qm)
        cons = constellation(qm)
        chk(len(cons) == 2 ** n, "Qm %d: constellation size %d" % (qm, len(cons)))
        # Gray property: adjacent levels differ in exactly one bit
        lv = sorted(cons, key=lambda x: x[1])
        gray_ok = all(sum(a != b for a, b in zip(lv[i][0], lv[i + 1][0])) == 1
                      for i in range(len(lv) - 1))
        chk(gray_ok, "Qm %d: constellation is not Gray-labelled" % qm)
        # average power per branch matches QPSK within 1%
        avg = sum(l * l for _, l in cons) / len(cons)
        a = LEVEL_SCALE[qm]
        chk(abs(avg * a * a - QPSK_AMP ** 2) < 0.02 * QPSK_AMP ** 2,
            "Qm %d: average power %.0f != %d" % (qm, avg * a * a, QPSK_AMP ** 2))
        print("  Qm=%d: %d levels, a=%d, avg power=%.0f (QPSK %d), Gray=%s"
              % (qm, len(cons), a, avg * a * a, QPSK_AMP ** 2, gray_ok))

        # map -> hard-decide round trip with a trivial channel h = (16, 0)
        h = complex(16, 0)
        nbits = qm * 240
        bits = [rnd.randrange(2) for _ in range(nbits)]
        syms = qam_map(bits, qm)
        chk(len(syms) * qm == nbits, "Qm %d: symbol count wrong" % qm)
        back = []
        for s in syms:
            back += qam_hard(s, h, qm)
        chk(back == bits, "Qm %d: noiseless hard-decision round trip failed" % qm)
        print("    %d symbols, noiseless map -> LLR -> hard: %d/%d bits OK"
              % (len(syms), nbits - sum(a != b for a, b in zip(back, bits)), nbits))

        # LLR sign must match the bit at moderate noise, for a nontrivial h
        h = complex(31, 15)
        h2 = int(h.real) ** 2 + int(h.imag) ** 2
        a = LEVEL_SCALE[qm]
        bits2 = [rnd.randrange(2) for _ in range(qm * 200)]
        hit = 0
        for s in qam_map(bits2, qm):
            pass
        for i, s in enumerate(qam_map(bits2, qm)):
            y = s * h / 16.0 + complex(rnd.gauss(0, 60), rnd.gauss(0, 60))
            ll = qam_llr(y, h, qm)
            hit += sum(1 for k in range(qm) if (1 if ll[k] < 0 else 0) == bits2[i * qm + k])
        rate = hit / len(bits2)
        chk(rate > 0.97, "Qm %d: LLR signs only %.3f correct" % (qm, rate))
        print("    noisy (h=31+15j, sigma=60) LLR signs: %.3f correct" % rate)

    # 16QAM LLR reduces to the known QPSK shape at Qm=2
    h = complex(32, 0)
    q = qam_llr(complex(2000 * 16, 0), h, 2)
    chk(len(q) == 2 and q[0] > 0, "Qm 2 LLR degenerated: %s" % q)

    # injection: wrong Qm must be rejected
    try:
        qam_map([0, 1, 0], 5)
        chk(False, "unsupported Qm was accepted")
    except ValueError:
        pass
    print("  injection: unsupported Qm -> ValueError")

    print("[ %s ] QPSK / 16QAM / 64QAM mapping + max-log LLR" % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(selftest())
