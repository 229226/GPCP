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
"""Minimal RRC connection control + MAC control elements (P3 of docs/project/README.md).

This is the L3 entry point after P1 (SIB1) and P2 (4-step RACH): the UE has a
C-RNTI from Msg4 and now establishes an RRC connection.

    SRB0 / CCCH : RRCSetupRequest  (UE -> gNB, UL-SCH)
    SRB0 / CCCH : RRCSetup          (gNB -> UE, DL-SCH, carries SRB1 config)
    SRB1 / DCCH : RRCSetupComplete (UE -> gNB, UL-SCH)
    SRB1 / DCCH : RRCReconfiguration / RRCReconfigurationComplete
    UL-SCH      : SR (PUCCH, 1 bit) -> UL grant -> BSR / PHR MAC CEs
    DL-SCH      : Timing Advance Command MAC CE (TA maintenance)

Deliberate simplification (same convention as the compact SIB1 of P1 and the
compact Msg3 of P2): each RRC message is a **fixed-order 64-bit SDU**, not a
38.331 ASN.1 PER encoding.  Full PER is deliberately deferred (docs/project/README.md P6).
Every SDU is wrapped in a 10-byte MAC PDU (1-byte subheader + 8-byte SDU +
1-byte padding) = exactly A_TB = 80 bits, so the already hardware-verified
PDSCH / PUSCH engines can carry it unchanged.

What is genuinely modelled:
  * SRB0 -> SRB1 transition (RRCSetup installs the SRB1 configuration);
  * transaction identifiers and the establishment cause;
  * Short BSR / PHR / Timing Advance Command MAC CEs with the 38.321 LCID
    values for DL-SCH / UL-SCH;
  * UE identity, C-RNTI and TA carried field-by-field and checked.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import nr_mac as MAC

# ---------------------------------------------------------------- constants
RRC_SDU_BITS = 64
RRC_SDU_BYTES = RRC_SDU_BITS // 8
RRC_PDU_BYTES = 10                     # 1 subheader + 8 SDU + 1 padding
TB_BITS = 80

# 38.321 LCID values used here (compact subset).
LCID_CCCH = MAC.LCID_CCCH              # 24, project convention for UL CCCH
LCID_DCCH = 1                          # SRB1 (DCCH)
LCID_SHORT_BSR = 61                    # UL-SCH, 38.321 Table 6.2.1-1
LCID_PHR = 57                          # UL-SCH (single-entry PHR)
LCID_TA_CMD = 61                       # DL-SCH, 38.321 Table 6.2.1-2
LCID_C_RNTI = 58                       # DL-SCH (C-RNTI CE)
LCID_PADDING = MAC.LCID_PADDING        # 63

# 38.331 RRC message types carried in the compact SDU header.
MSG_SETUP_REQUEST = 1
MSG_SETUP = 2
MSG_SETUP_COMPLETE = 3
MSG_RECONFIGURATION = 4
MSG_RECONFIGURATION_COMPLETE = 5
MSG_NAMES = {MSG_SETUP_REQUEST: "RRCSetupRequest",
             MSG_SETUP: "RRCSetup",
             MSG_SETUP_COMPLETE: "RRCSetupComplete",
             MSG_RECONFIGURATION: "RRCReconfiguration",
             MSG_RECONFIGURATION_COMPLETE: "RRCReconfigurationComplete"}

ESTABLISHMENT_CAUSES = {0: "emergency", 1: "highPriorityAccess", 2: "mt-Access",
                        3: "mo-Signalling", 4: "mo-Data", 5: "delayTolerantAccess",
                        6: "mo-VoiceCall", 7: "spare"}

# Compact 64-bit SDU layouts.  Common header: msg_type(4) | transaction_id(2) |
# spare(2) = 8 bits, then 56 bits of type-specific fields.
COMMON_FIELDS = [("msg_type", 4), ("transaction_id", 2), ("spare", 2)]

SDU_FIELDS = {
    MSG_SETUP_REQUEST: [
        ("ue_identity", 40), ("establishment_cause", 4),
        ("preamble_used", 6), ("reserved", 6)],
    MSG_SETUP: [
        ("c_rnti", 16), ("srb1_rlc_mode", 2), ("srb1_pdcp_sn_len", 2),
        ("ta_command", 12), ("bwp_id", 2), ("pucch_resource", 6),
        ("sr_periodicity", 4), ("reserved", 12)],
    MSG_SETUP_COMPLETE: [
        ("selected_plmn", 8), ("amf_id", 16), ("nas_pdu_len", 8),
        ("srb1_active", 1), ("reserved", 23)],
    MSG_RECONFIGURATION: [
        ("cell_group_id", 4), ("drb_id", 4), ("lcid", 6),
        ("ta_command", 12), ("sr_resource", 6), ("meas_gap", 2),
        ("reserved", 22)],
    MSG_RECONFIGURATION_COMPLETE: [
        ("drb_id", 4), ("srb1_active", 1), ("reserved", 51)],
}
for _k, _f in SDU_FIELDS.items():
    assert sum(w for _, w in COMMON_FIELDS) + sum(w for _, w in _f) == RRC_SDU_BITS, \
        "SDU layout for %s must be %d bits" % (_k, RRC_SDU_BITS)


# ---------------------------------------------------------------- RRC SDUs
def pack_sdu(msg_type, **fields):
    """Pack a compact 64-bit RRC SDU (MSB first)."""
    if msg_type not in SDU_FIELDS:
        raise ValueError("unknown RRC message type %r" % msg_type)
    layout = COMMON_FIELDS + SDU_FIELDS[msg_type]
    vals = dict(fields)
    vals.setdefault("msg_type", msg_type)
    vals.setdefault("transaction_id", 0)
    vals.setdefault("spare", 0)
    for name, _w in layout:
        vals.setdefault(name, 0)
    v = 0
    for name, w in layout:
        x = int(vals[name])
        if x < 0 or x >= (1 << w):
            raise ValueError("%s=%d does not fit in %d bits" % (name, x, w))
        v = (v << w) | x
    return v.to_bytes(RRC_SDU_BYTES, "big")


def unpack_sdu(sdu):
    """Inverse of pack_sdu; returns a dict including 'msg_type'."""
    if len(sdu) != RRC_SDU_BYTES:
        raise ValueError("RRC SDU must be %d bytes, got %d" % (RRC_SDU_BYTES, len(sdu)))
    v = int.from_bytes(sdu, "big")
    msg_type = v >> (RRC_SDU_BITS - COMMON_FIELDS[0][1])
    if msg_type not in SDU_FIELDS:
        raise ValueError("unknown RRC message type %d" % msg_type)
    layout = COMMON_FIELDS + SDU_FIELDS[msg_type]
    out = {}
    for name, w in reversed(layout):
        out[name] = v & ((1 << w) - 1)
        v >>= w
    return out


def fit_tb(entries):
    """Pack MAC SDUs/CEs into exactly RRC_PDU_BYTES (80-bit TB).

    Each subheader costs 2 bytes, so a single 8-byte RRC SDU already fills the
    TB with no padding; shorter CEs get a padding subheader plus zero bytes.
    """
    base = MAC.pack_pdu(entries)
    if len(base) > RRC_PDU_BYTES:
        raise ValueError("MAC PDU is %d bytes, exceeds the %d-bit TB"
                         % (len(base), TB_BITS))
    if len(base) == RRC_PDU_BYTES:
        return base
    need = RRC_PDU_BYTES - len(base) - 2      # padding subheader itself
    if need < 0:
        raise ValueError("no room for a padding subheader")
    return MAC.pack_pdu(entries + [(LCID_PADDING, bytes(need))])


def pack_pdu(msg_type, lcid=None, **fields):
    """Wrap an RRC SDU in a 10-byte MAC PDU padded to the 80-bit TB."""
    sdu = pack_sdu(msg_type, **fields)
    if lcid is None:
        lcid = LCID_CCCH if msg_type == MSG_SETUP_REQUEST else LCID_DCCH
    return fit_tb([(lcid, sdu)])


def unpack_pdu(pdu):
    """Return (lcid, RRC-field dict) from a 10-byte MAC PDU."""
    for lcid, payload in MAC.unpack_pdu(pdu):
        if lcid == LCID_PADDING:
            continue
        if len(payload) == RRC_SDU_BYTES:
            return lcid, unpack_sdu(payload)
    raise ValueError("no RRC SDU found in MAC PDU")


def pdu_to_tb(pdu):
    assert len(pdu) * 8 == TB_BITS
    bits = []
    for b in pdu:
        bits += [(b >> (7 - i)) & 1 for i in range(8)]
    return bits


def tb_to_pdu(bits):
    by = bytearray()
    for i in range(0, TB_BITS, 8):
        v = 0
        for b in bits[i:i + 8]:
            v = (v << 1) | b
        by.append(v)
    return bytes(by)


# ---------------------------------------------------------------- MAC CEs
def pack_short_bsr(lcg_id, buffer_size):
    """38.321 6.1.3.1: Short BSR = LCG ID(3) | Buffer Size(5), one octet."""
    if not 0 <= lcg_id < 8 or not 0 <= buffer_size < 32:
        raise ValueError("Short BSR fields out of range")
    return fit_tb([(LCID_SHORT_BSR, bytes([(lcg_id << 5) | buffer_size]))])


def parse_short_bsr(pdu):
    for lcid, payload in MAC.unpack_pdu(pdu):
        if lcid == LCID_SHORT_BSR and len(payload) == 1:
            return dict(lcg_id=payload[0] >> 5, buffer_size=payload[0] & 0x1F)
    return None


def pack_phr(ph, p_cmax=None):
    """38.321 6.1.3.8: single-entry PHR = R,R | PH(6) [| R,R | P_CMAX(6)]."""
    if not 0 <= ph < 64:
        raise ValueError("PH field must be 6 bits")
    payload = bytes([ph & 0x3F]) if p_cmax is None else bytes([ph & 0x3F, p_cmax & 0x3F])
    return fit_tb([(LCID_PHR, payload)])


def parse_phr(pdu):
    for lcid, payload in MAC.unpack_pdu(pdu):
        if lcid == LCID_PHR and payload:
            return dict(ph=payload[0] & 0x3F)
    return None


def pack_ta_cmd(tag_id, ta_command):
    """38.321 6.1.3.4: TA Command CE = R,R | TAG ID(2) | TA command(6)."""
    if not 0 <= tag_id < 4 or not 0 <= ta_command < 64:
        raise ValueError("TA command CE fields out of range")
    return fit_tb([(LCID_TA_CMD, bytes([(tag_id << 6) | (ta_command & 0x3F)]))])


def parse_ta_cmd(pdu):
    for lcid, payload in MAC.unpack_pdu(pdu):
        if lcid == LCID_TA_CMD and len(payload) == 1:
            return dict(tag_id=(payload[0] >> 6) & 3, ta_command=payload[0] & 0x3F)
    return None


def pack_c_rnti_ce(c_rnti):
    """38.321 6.1.3.2: C-RNTI CE = 2 octets."""
    return fit_tb([(LCID_C_RNTI, c_rnti.to_bytes(2, "big"))])


def parse_c_rnti_ce(pdu):
    for lcid, payload in MAC.unpack_pdu(pdu):
        if lcid == LCID_C_RNTI and len(payload) == 2:
            return int.from_bytes(payload, "big")
    return None


# ---------------------------------------------------------------- SRB state
class RrcEntity:
    """UE/gNB RRC state machine: SRB0 -> SRB1 and TA maintenance."""

    def __init__(self, ue_identity=0, c_rnti=0, ta=0):
        self.ue_identity = ue_identity
        self.c_rnti = c_rnti
        self.ta = ta
        self.state = "IDLE"
        self.srb1_active = False
        self.transaction_id = 0
        self.srb1_rlc_mode = 0            # 0 = AM
        self.srb1_pdcp_sn_len = 1         # 12 bit
        self.drb_id = 0
        self.buffer_bytes = 0

    def setup_request(self, preamble, cause=3):
        self.state = "SETUP_REQ_SENT"
        self.transaction_id = 0
        return pack_pdu(MSG_SETUP_REQUEST, ue_identity=self.ue_identity,
                        establishment_cause=cause, preamble_used=preamble)

    def setup(self, ta_command=None):
        self.transaction_id = 1
        ta = self.ta if ta_command is None else ta_command
        self.ta = ta
        return pack_pdu(MSG_SETUP, transaction_id=self.transaction_id,
                        c_rnti=self.c_rnti, srb1_rlc_mode=self.srb1_rlc_mode,
                        srb1_pdcp_sn_len=self.srb1_pdcp_sn_len,
                        ta_command=ta & 0xFFF, bwp_id=0, pucch_resource=1,
                        sr_periodicity=4)

    def apply_setup(self, fields):
        """UE installs the SRB1 configuration carried by RRCSetup."""
        ok = (fields["msg_type"] == MSG_SETUP
              and fields["c_rnti"] == self.c_rnti
              and fields["transaction_id"] == 1)
        if ok:
            self.srb1_active = True
            self.srb1_rlc_mode = fields["srb1_rlc_mode"]
            self.srb1_pdcp_sn_len = fields["srb1_pdcp_sn_len"]
            self.state = "SRB1_CONFIGURED"
        return ok

    def setup_complete(self, amf_id=0x1234, nas_pdu_len=16):
        if not self.srb1_active:
            raise RuntimeError("RRCSetupComplete requires SRB1")
        self.state = "CONNECTED"
        return pack_pdu(MSG_SETUP_COMPLETE, lcid=LCID_DCCH,
                        selected_plmn=1, amf_id=amf_id,
                        nas_pdu_len=nas_pdu_len, srb1_active=1)

    def reconfiguration(self, drb_id=1, lcid=4, ta_command=None):
        self.transaction_id = 2
        ta = self.ta if ta_command is None else ta_command
        self.drb_id = drb_id
        return pack_pdu(MSG_RECONFIGURATION, transaction_id=self.transaction_id,
                        cell_group_id=0, drb_id=drb_id, lcid=lcid,
                        ta_command=ta & 0xFFF, sr_resource=2, meas_gap=0)

    def apply_reconfiguration(self, fields, ta_ce=None):
        ok = (fields["msg_type"] == MSG_RECONFIGURATION
              and fields["transaction_id"] == 2 and self.srb1_active)
        if ok:
            self.drb_id = fields["drb_id"]
            if ta_ce is not None:
                self.ta = ta_ce["ta_command"]
            self.state = "CONNECTED_RECONFIGURED"
        return ok

    def reconfiguration_complete(self):
        return pack_pdu(MSG_RECONFIGURATION_COMPLETE, lcid=LCID_DCCH,
                        drb_id=self.drb_id, srb1_active=1)


# ---------------------------------------------------------------- self test
def selftest():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        if not cond:
            print("  " + msg)
            ok = False

    print("RRC / MAC CE golden model self-test (compact %d-bit SDU, %d-bit TB)"
          % (RRC_SDU_BITS, TB_BITS))

    # SDU round trip for every message type
    cases = {
        MSG_SETUP_REQUEST: dict(ue_identity=0x00A1B2C3D4,
                                establishment_cause=3, preamble_used=2),
        MSG_SETUP: dict(c_rnti=0x7002, srb1_rlc_mode=0, srb1_pdcp_sn_len=1,
                        ta_command=9, bwp_id=0, pucch_resource=1,
                        sr_periodicity=4, transaction_id=1),
        MSG_SETUP_COMPLETE: dict(selected_plmn=1, amf_id=0x1234,
                                 nas_pdu_len=16, srb1_active=1),
        MSG_RECONFIGURATION: dict(cell_group_id=0, drb_id=1, lcid=4,
                                  ta_command=11, sr_resource=2, meas_gap=0,
                                  transaction_id=2),
        MSG_RECONFIGURATION_COMPLETE: dict(drb_id=1, srb1_active=1),
    }
    for mt, fields in cases.items():
        sdu = pack_sdu(mt, **fields)
        back = unpack_sdu(sdu)
        chk(back["msg_type"] == mt, "%s: msg_type round-trip failed" % MSG_NAMES[mt])
        for k, v in fields.items():
            chk(back[k] == v, "%s: %s %d != %d" % (MSG_NAMES[mt], k, back[k], v))
        pdu = pack_pdu(mt, **fields)
        chk(len(pdu) == RRC_PDU_BYTES,
            "%s: PDU is %d bytes, want %d" % (MSG_NAMES[mt], len(pdu), RRC_PDU_BYTES))
        lcid, got = unpack_pdu(pdu)
        chk(got["msg_type"] == mt, "%s: PDU round-trip failed" % MSG_NAMES[mt])
        chk(tb_to_pdu(pdu_to_tb(pdu)) == pdu, "%s: TB round-trip failed" % MSG_NAMES[mt])
    print("  RRC SDU/PDU/TB round-trip: %d message types OK" % len(cases))

    # MAC CE round trips
    chk(parse_short_bsr(pack_short_bsr(3, 17)) == dict(lcg_id=3, buffer_size=17),
        "Short BSR round-trip failed")
    chk(parse_phr(pack_phr(42)) == dict(ph=42), "PHR round-trip failed")
    chk(parse_ta_cmd(pack_ta_cmd(1, 9)) == dict(tag_id=1, ta_command=9),
        "TA Command CE round-trip failed")
    chk(parse_c_rnti_ce(pack_c_rnti_ce(0x7002)) == 0x7002,
        "C-RNTI CE round-trip failed")
    print("  MAC CE (Short BSR / PHR / TA cmd / C-RNTI): round-trip OK")

    # SRB0 -> SRB1 state machine
    e = RrcEntity(ue_identity=0xA1B2C3D4, c_rnti=0x7002, ta=9)
    req = e.setup_request(preamble=2)
    _, req_f = unpack_pdu(req)
    chk(req_f["ue_identity"] == e.ue_identity, "SetupRequest UE identity lost")
    chk(not e.srb1_active and e.state == "SETUP_REQ_SENT", "pre-setup state wrong")
    setup = e.setup()
    _, st_f = unpack_pdu(setup)
    chk(e.apply_setup(st_f), "RRCSetup rejected")
    chk(e.srb1_active and e.state == "SRB1_CONFIGURED", "SRB1 not installed")
    comp = e.setup_complete()
    _, cp_f = unpack_pdu(comp)
    chk(cp_f["srb1_active"] == 1 and e.state == "CONNECTED",
        "RRCSetupComplete / CONNECTED state wrong")
    recfg = e.reconfiguration(drb_id=2, ta_command=11)
    _, rc_f = unpack_pdu(recfg)
    chk(e.apply_reconfiguration(rc_f, ta_ce=parse_ta_cmd(pack_ta_cmd(0, 11))),
        "RRCReconfiguration rejected")
    chk(e.ta == 11 and e.drb_id == 2 and e.state == "CONNECTED_RECONFIGURED",
        "TA / DRB maintenance wrong")
    print("  RRC state machine: IDLE -> SRB1_CONFIGURED -> CONNECTED "
          "-> CONNECTED_RECONFIGURED (TA %d -> %d) OK" % (9, e.ta))

    # A wrong C-RNTI must be rejected (real gate, not decorative)
    bad = dict(st_f)
    bad["c_rnti"] = 0x1234
    e2 = RrcEntity(c_rnti=0x7002)
    chk(not e2.apply_setup(bad), "wrong C-RNTI was accepted")
    print("  injection: wrong C-RNTI in RRCSetup -> rejected")

    print("[ %s ] RRC compact messages + MAC CEs + SRB0->SRB1 state" %
          ("OK" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    import argparse
    ap = argparse.ArgumentParser(description="RRC / MAC CE compact golden model")
    ap.add_argument("cmd", nargs="?", default="selftest")
    a = ap.parse_args()
    return selftest()


if __name__ == "__main__":
    raise SystemExit(main())
