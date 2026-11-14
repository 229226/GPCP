#!/usr/bin/env python3
"""Minimal NR MAC / HARQ entity skeleton (38.321).

This is the L2 entry point called out in AGENTS.md §6/P3.  It deliberately
stops at what can be verified without a full stack:

    MAC PDU  : subheader (LCID / F / E / L) + SDU packing and unpacking
    HARQ     : 8-process entity with NDI toggling and the 0->2->3->1 RV cycle
    RAR      : Msg2 random-access response PDU (BI/RAPID subheaders, TA, 27-bit
               UL grant, Temporary C-RNTI) and the Msg4 UE Contention
               Resolution Identity CE

No scheduling, no logical-channel prioritisation, no RLC segmentation, no
PDCP ciphering.  It is the interface skeleton that a PDCCH/PDSCH chain can
later drive: dci_for(pid) produces the exact MCS/RV/NDI fields that
tools/nr_pdcch.py already knows how to transmit.
"""
import random

NPROC = 8
RV_CYCLE = (0, 2, 3, 1)
LCID_MASK = 0x3F

# 38.321 Table 6.1.3: CCCH and padding are fixed LCID values in the 6-bit space.
LCID_CCCH = 0b11000          # 24
LCID_PADDING = 0b111111      # 63


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


# ---------------------------------------------------------------- RAR (Msg2)
# 38.321 6.2.3: a RAR MAC PDU is a sequence of *fixed-size* 1-octet subheaders
# (R | E/T | RAPID-or-BI) followed by the RAR body.  The body is
#   TA (12) | UL grant (27) | reserved (1) | Temporary C-RNTI (24) = 64 bits.
# One BI subheader + one RAPID subheader + 8 bytes of RAR = exactly 10 bytes,
# which is the 80-bit transport block the verified PDSCH engine carries.
#
# The E and T bits share one position in the spec octet.  Here T=1 marks the BI
# subheader and a RAPID subheader always has E=0 (single RAR per random-access
# response), so the field is unambiguous; a multi-RAR PDU would need the full
# 38.321 disambiguation, which this model does not exercise.
RAR_SUBHDR_BITS = 6
RAR_UL_GRANT_FIELDS = [("fh", 1), ("rb_start", 6), ("rb_len", 3), ("tda", 4),
                       ("mcs", 4), ("harq_timing", 3), ("tpc", 3),
                       ("power_class", 2), ("reserved", 1)]
RAR_UL_GRANT_BITS = sum(w for _, w in RAR_UL_GRANT_FIELDS)     # 27
RAR_TA_BITS = 12
RAR_TC_RNTI_BITS = 24
RAR_BODY_BITS = RAR_TA_BITS + RAR_UL_GRANT_BITS + 1 + RAR_TC_RNTI_BITS   # 64
RAR_PDU_BYTES = 2 + RAR_BODY_BITS // 8                                     # 10


def pack_ul_grant(g):
    """RAR UL grant: the 27-bit field group of 38.321 Table 6.2.3-2 (compact)."""
    v = 0
    for name, w in RAR_UL_GRANT_FIELDS:
        x = int(g.get(name, 0))
        if x < 0 or x >= (1 << w):
            raise ValueError("%s=%d does not fit in %d bits" % (name, x, w))
        v = (v << w) | x
    return v


def unpack_ul_grant(v):
    out = {}
    for name, w in reversed(RAR_UL_GRANT_FIELDS):
        out[name] = v & ((1 << w) - 1)
        v >>= w
    return out


def pack_rar(rapid, ta, ul_grant, tc_rnti, bi=0):
    """Msg2 RAR MAC PDU (10 bytes)."""
    for name, val, bits in (("RAPID", rapid, 6), ("TA", ta, RAR_TA_BITS),
                            ("TC-RNTI", tc_rnti, RAR_TC_RNTI_BITS),
                            ("BI", bi, 6)):
        if not 0 <= val < (1 << bits):
            raise ValueError("%s=%d does not fit in %d bits" % (name, val, bits))
    body = (ta << (RAR_UL_GRANT_BITS + 1 + RAR_TC_RNTI_BITS)
            | pack_ul_grant(ul_grant) << (1 + RAR_TC_RNTI_BITS)
            | tc_rnti)
    return (bytes([(1 << RAR_SUBHDR_BITS) | bi, rapid])            # BI, RAPID
            + body.to_bytes(RAR_BODY_BITS // 8, "big"))


def parse_rar(pdu):
    """Inverse of pack_rar; returns the decoded Msg2 fields."""
    if len(pdu) != RAR_PDU_BYTES:
        raise ValueError("RAR PDU must be %d bytes, got %d"
                         % (RAR_PDU_BYTES, len(pdu)))
    if not pdu[0] & (1 << RAR_SUBHDR_BITS):
        raise ValueError("RAR PDU must start with the BI subheader")
    bi, rapid = pdu[0] & ((1 << RAR_SUBHDR_BITS) - 1), pdu[1]
    body = int.from_bytes(pdu[2:], "big")
    tc_rnti = body & ((1 << RAR_TC_RNTI_BITS) - 1)
    body >>= RAR_TC_RNTI_BITS + 1                      # drop the reserved bit
    grant_bits = body & ((1 << RAR_UL_GRANT_BITS) - 1)
    ta = body >> RAR_UL_GRANT_BITS
    return dict(bi=bi, rapid=rapid, ta=ta, tc_rnti=tc_rnti,
                ul_grant=unpack_ul_grant(grant_bits))


# ------------------------------------------------ Msg4 contention resolution
CR_ID_BITS = 48            # 38.321 6.2.1: UE Contention Resolution Identity


def pack_cr_pdu(ccch_sdu):
    """Msg4 PDU: CCCH subheader + the first 6 bytes of the Msg3 CCCH SDU.

    Padded to the 10-byte (80-bit) transport block with a padding subheader, so
    the same verified PDSCH engine that carries RAR also carries Msg4.
    """
    ce = ccch_sdu[:CR_ID_BITS // 8]
    pdu = pack_pdu([(LCID_CCCH, ce), (LCID_PADDING, b"")])
    assert len(pdu) == RAR_PDU_BYTES, "Msg4 PDU must stay %d bytes" % RAR_PDU_BYTES
    return pdu


def parse_cr_pdu(pdu):
    """Return the contention-resolution identity, or None if absent."""
    for lcid, payload in unpack_pdu(pdu):
        if lcid == LCID_CCCH:
            return payload
    return None


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

    # RAR (Msg2) and the Msg4 contention-resolution CE, both 10 bytes = A_TB
    grant = dict(fh=0, rb_start=3, rb_len=5, tda=1, mcs=9, harq_timing=4,
                 tpc=2, power_class=1, reserved=0)
    rar = pack_rar(rapid=21, ta=137, ul_grant=grant, tc_rnti=0xA3B1C2, bi=7)
    back = parse_rar(rar)
    rar_ok = (len(rar) == RAR_PDU_BYTES and back["bi"] == 7
              and back["rapid"] == 21 and back["ta"] == 137
              and back["tc_rnti"] == 0xA3B1C2 and back["ul_grant"] == grant)
    sdu3 = b"abcdefgh"
    cr = pack_cr_pdu(sdu3)
    cr_ok = len(cr) == RAR_PDU_BYTES and parse_cr_pdu(cr) == sdu3[:6]
    print("RAR PDU: %d bytes, round-trip %s (TA=%d TC-RNTI=0x%06x grant=%s)" % (
        len(rar), "OK" if rar_ok else "BAD", back["ta"], back["tc_rnti"],
        back["ul_grant"]))
    print("Msg4 CE PDU: %d bytes, identity %s" % (
        len(cr), "OK" if cr_ok else "BAD"))

    ok = pdu_ok and rv_ok and ndi_ok and rar_ok and cr_ok
    print("[ %s ] MAC PDU + HARQ entity + RAR/Msg4 CEs" % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
