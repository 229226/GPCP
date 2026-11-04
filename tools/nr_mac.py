#!/usr/bin/env python3
"""Minimal NR MAC / HARQ entity skeleton (38.321).

This is the L2 entry point called out in AGENTS.md §6/P3.  It deliberately
stops at what can be verified without a full stack:

    MAC PDU  : subheader (LCID / F / E / L) + SDU packing and unpacking
    HARQ     : 8-process entity with NDI toggling and the 0->2->3->1 RV cycle

No scheduling, no logical-channel prioritisation, no RLC segmentation, no
PDCP ciphering.  It is the interface skeleton that a PDCCH/PDSCH chain can
later drive: dci_for(pid) produces the exact MCS/RV/NDI fields that
tools/nr_pdcch.py already knows how to transmit.
"""
import random

NPROC = 8
RV_CYCLE = (0, 2, 3, 1)
LCID_MASK = 0x3F


def pack_pdu(sdus):
    """sdus: list of (lcid:int, payload:bytes).  One subheader per SDU."""
    out = bytearray()
    for i, (lcid, sdu) in enumerate(sdus):
        assert 0 <= lcid <= LCID_MASK and len(sdu) <= 255
        e = 0 if i == len(sdus) - 1 else 1
        out.append((lcid & LCID_MASK) | (e << 7))
        out.append(len(sdu) & 0xFF)
        out += sdu
    return bytes(out)


def unpack_pdu(pdu):
    out, p = [], 0
    while p < len(pdu):
        b0 = pdu[p]
        l = pdu[p + 1]
        p += 2
        e = (b0 >> 7) & 1
        lcid = b0 & LCID_MASK
        out.append((lcid, pdu[p:p + l]))
        p += l
        if not e:
            break
    assert p == len(pdu), "trailing bytes in MAC PDU"
    return out


class HarqEntity:
    def __init__(self, nproc=NPROC):
        self.ndi = [0] * nproc
        self.rv_idx = [0] * nproc
        self.tb = [None] * nproc

    def new_tx(self, pid, tb, mcs=17):
        self.ndi[pid] ^= 1
        self.rv_idx[pid] = 0
        self.tb[pid] = tb
        return self.dci_for(pid, mcs)

    def retx(self, pid, mcs=17):
        self.rv_idx[pid] = (self.rv_idx[pid] + 1) % len(RV_CYCLE)
        return self.dci_for(pid, mcs)

    def dci_for(self, pid, mcs=17):
        return dict(mcs=mcs, rv=RV_CYCLE[self.rv_idx[pid]], ndi=self.ndi[pid],
                    harq_pid=pid, rb_start=0, rb_len=8, tpc=0, reserved=0)


def main():
    rnd = random.Random(11)
    sdus = [(1, bytes(rnd.randrange(256) for _ in range(24))),
            (4, bytes(rnd.randrange(256) for _ in range(37))),
            (2, b"gpcp-mac")]
    pdu = pack_pdu(sdus)
    back = unpack_pdu(pdu)
    pdu_ok = back == sdus

    h = HarqEntity()
    tb = b"transport-block-0"
    seq = [h.new_tx(3, tb)]
    seq += [h.retx(3) for _ in range(3)]
    seq += [h.new_tx(3, b"transport-block-1")]
    rvs = [d["rv"] for d in seq]
    ndis = [d["ndi"] for d in seq]
    rv_ok = rvs == [0, 2, 3, 1, 0]
    ndi_ok = ndis == [1, 1, 1, 1, 0]

    print("MAC PDU: %d SDUs -> %d bytes, round-trip %s" % (
        len(sdus), len(pdu), "OK" if pdu_ok else "BAD"))
    print("HARQ pid3: RV sequence %s %s, NDI %s %s" % (
        rvs, "OK" if rv_ok else "BAD", ndis, "OK" if ndi_ok else "BAD"))
    print("  DCI for first transmission: %s" % seq[0])
    ok = pdu_ok and rv_ok and ndi_ok
    print("[ %s ] MAC PDU + HARQ entity" % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
