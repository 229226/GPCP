"""Protocol numerology: derive waveform-level quantities from cfg().

This is the single place where protocol tables live.  Adding a new RAT means
adding a table module and extending derive(); workload scenarios never see
hardware numbers.  The v1 derivation is kept byte-identical to the original
hll._derive so the frozen regression lock still holds.
"""
from .nr import CP_RATIO, N_RB
from .lte import LTE_NOTE
from .wifi import WIFI_CP_RATIO


def derive(cfg):
    """Waveform-level derivation (schema v1, frozen)."""
    d = {}
    if "fft_points" in cfg:
        n = int(cfg["fft_points"])
        d["n_fft_min"] = n
        d["cp_len"] = int(cfg.get("cp_len", int(round(n * WIFI_CP_RATIO))))
        d["n_subcarriers"] = int(cfg.get("n_data_subcarriers", 0))
    bw = cfg.get("bw_mhz")
    scs = cfg.get("scs_khz")
    if bw is not None and scs is not None:
        key = (bw, scs)
        if key not in N_RB:
            raise ValueError(
                "cfg: 带宽/子载波间隔组合 %s 未登记（38.101-1），请先补表" % (key,))
        n_rb = N_RB[key]
        need = n_rb * 12
        d["n_rb"] = n_rb
        d["n_subcarriers"] = need
        n = 1
        while n < need:
            n <<= 1
        d["n_fft_min"] = n
        d["cp_len"] = int(round(n * CP_RATIO))
    for k in ("n_id", "n_rnti", "qm", "al", "reg", "e_bits", "a_bits"):
        if k in cfg:
            d[k] = cfg[k]
    return d


TABLES = {"nr": "nr.N_RB", "lte": LTE_NOTE, "wifi": "wifi.WIFI_CP_RATIO"}
