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
"""PDCCH REG bundling / interleaving / CCE-to-REG mapping (38.211 7.3.2.3-4).

The project's PDCCH gates (run-pdcch, run-link-e2e, run-sib1, run-rach) use a
flat 432-LLR E sequence and skip the physical REG/CCE step.  This module adds
the missing piece at model level so the mapping can be checked and later
dropped into the hardware path.

38.211 7.3.2.3 (interleaved mapping)
    N_REG   REGs in the CORESET, L = bundle size (2 or 6)
    C = N_REG / L          (number of bundles)
    R = 2                  (rows; the spec fixes two rows)
    j = c*R + r            (bundle index before interleaving)
    f(j) = (r*C + c + n_shift) mod C
    REGs of bundle j -> bundle f(j)

38.211 7.3.2.4 (CCE-to-REG)
    one CCE = 6 REGs, i.e. L=6 uses one bundle, L=2 uses three bundles.
"""
import argparse
import os
import sys

R = 2                     # the spec fixes two rows for the block interleaver
REG_PER_CCE = 6


def bundles(n_reg, L):
    if n_reg % L:
        raise ValueError("N_REG=%d not a multiple of L=%d" % (n_reg, L))
    return n_reg // L


def interleave(n_reg, L, n_shift=0):
    """Return f: bundle index -> physical bundle index (a permutation).

    Matrix is R rows x C columns with C = N_bundle/R; the permutation is
    f(j) = (r*C + c + n_shift) mod N_bundle with j = c*R + r.  The modulus is
    the *bundle count*, not the column count.
    """
    nb = bundles(n_reg, L)
    if nb % R:
        raise ValueError("N_bundle=%d is not even (N_REG=%d, L=%d)"
                         % (nb, n_reg, L))
    C = nb // R
    f = [0] * nb
    for j in range(nb):
        c, r = divmod(j, R)
        f[j] = (r * C + c + n_shift) % nb
    return f


def deinterleave(n_reg, L, n_shift=0):
    f = interleave(n_reg, L, n_shift)
    inv = [0] * len(f)
    for j, p in enumerate(f):
        inv[p] = j
    return inv


def bundle_regs(n_reg, L, bundle):
    return list(range(bundle * L, (bundle + 1) * L))


def cce_regs(cce, n_reg, L, n_shift=0):
    """Physical REG indices carrying one CCE, in increasing CCE order."""
    f = interleave(n_reg, L, n_shift)
    nb = REG_PER_CCE // L
    out = []
    for b in range(nb):
        j = cce * nb + b
        out += bundle_regs(n_reg, L, f[j])
    return out


def selftest():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        if not cond:
            print("  " + msg)
            ok = False

    # CORESET sizes used by the project: 24 RB x 1..3 symbols -> multiples of 6
    for n_reg, L in ((24, 2), (24, 6), (48, 2), (48, 6), (72, 6), (96, 2)):
        f = interleave(n_reg, L)
        chk(sorted(f) == list(range(len(f))),
            "f is not a permutation (N_REG=%d L=%d)" % (n_reg, L))
        inv = deinterleave(n_reg, L)
        chk(all(inv[f[j]] == j for j in range(len(f))),
            "deinterleave does not invert (N_REG=%d L=%d)" % (n_reg, L))
        # CCEs must tile all REGs exactly once
        n_cce = n_reg // REG_PER_CCE
        used = []
        for c in range(n_cce):
            used += cce_regs(c, n_reg, L)
        chk(sorted(used) == list(range(n_reg)),
            "CCEs do not tile the CORESET (N_REG=%d L=%d)" % (n_reg, L))
        print("  N_REG=%2d L=%d: %d bundles, %d CCEs, f=%s"
              % (n_reg, L, len(f), n_cce, f[:8]))

    # n_shift must actually move the mapping
    a = interleave(48, 6, 0)
    b = interleave(48, 6, 3)
    chk(a != b, "n_shift has no effect")
    # and the shift is a rotation of the bundle order
    chk(all((a[i] + 3) % len(a) == b[i] for i in range(len(a))),
        "n_shift is not a rotation")

    # L=6: one bundle per CCE, so CCE c owns exactly bundle f(c)
    f = interleave(48, 6)
    chk(cce_regs(0, 48, 6) == list(range(f[0] * 6, f[0] * 6 + 6)),
        "L=6 CCE mapping is not one bundle")
    # L=2: three bundles per CCE, spaced by C/3 in the interleaved order
    regs = cce_regs(1, 48, 2)
    chk(len(regs) == 6 and len(set(regs)) == 6, "L=2 CCE is not 6 distinct REGs")

    print("[ %s ] PDCCH REG/CCE mapping (38.211 7.3.2.3-4)"
          % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="PDCCH REG/CCE mapping model")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("selftest")
    a = ap.parse_args()
    if a.cmd in (None, "selftest"):
        return selftest()
    return 2


if __name__ == "__main__":
    sys.exit(main())
