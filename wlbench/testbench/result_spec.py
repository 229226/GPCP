"""wlbench 结果字契约（与 include/wlbench.h 的 enum 一一对应）。

改动 C 侧顺序时必须同步本文件；check.py 会用这里的字段名和容差分类。
"""

# 头部：必须逐值相等
HEADER = {
    0: ("magic", "exact"),
    1: ("schema", "exact"),
    2: ("slots", "exact"),
    3: ("ues", "exact"),
}

# 单字字段：(下标, 名称, 容差类别)
SCALARS = [
    (4,  "nr_dl_tb", "count"),
    (5,  "nr_dl_tb_first", "count"),
    (6,  "nr_dl_retx", "count"),
    (7,  "nr_dl_nack", "count"),
    (8,  "nr_dl_nack_first", "count"),
    (9,  "nr_rank2", "count"),
    (10, "nr_layers_sum", "count"),

    (13, "nr_ul_tb", "count"),
    (14, "nr_ul_tb_first", "count"),
    (15, "nr_ul_nack", "count"),
    (16, "nr_ul_nack_first", "count"),
    (17, "nr_ul_retx", "count"),
    (18, "nr_ul_drop", "count"),

    (21, "nr_pdcch", "count"),
    (22, "nr_ssb", "count"),
    (23, "nr_sib1", "count"),
    (24, "nr_prach", "count"),
    (25, "nr_ra_ok", "count"),
    (26, "nr_ra_fail", "count"),

    (27, "lte_tb", "count"),
    (28, "lte_nack", "count"),

    (31, "wifi_attempt", "count"),
    (32, "wifi_coll", "count"),

    (35, "nr_nack_ppm", "ppm"),
    (36, "nr_first_bler_ppm", "ppm"),
    (37, "nr_exp_bler_ppm", "ppm"),
    (38, "ul_nack_ppm", "ppm"),
    (39, "ul_first_bler_ppm", "ppm"),
    (40, "ul_exp_bler_ppm", "ppm"),
    (41, "lat_mean_mslot", "mslot"),
    (42, "lat_max_mslot", "mslot"),
    (43, "ul_lat_mean_mslot", "mslot"),
    (44, "jain_ppm", "ppm"),
    (45, "fs_gain_ppm", "ppm_signed"),
    (46, "cce_util_ppm", "ppm"),
    (47, "cce_blocked", "count"),
    (48, "olla_mean_mdb", "mdb"),
    (49, "ul_power_mean_mdb", "mdb"),
    (50, "ul_sinr_mean_mdb", "mdb"),
    (51, "layers_mean_milli", "milli"),
    (52, "rbg_per_tb_milli", "milli"),
    (53, "spread_mdb", "mdb"),
    (54, "rbg_spread_mdb", "mdb"),
    (55, "config_hash", "exact"),

    # ---- PHY 结果块（schema 2）：216 起，追加在 UE 区之后 ----
    (216, "phy_tb_ok", "count"),
    (217, "phy_tb_fail", "count"),
    (220, "phy_bler_ppm", "ppm"),
    (221, "phy_raw_ber_ppm", "ppm"),
    (222, "phy_post_ber_ppm", "ppm"),
    (223, "phy_evm_mdb", "mdb"),
    (224, "phy_nmse_mdb", "mdb"),
    (225, "phy_snr_est_mdb", "mdb"),
    (226, "phy_rate_milli", "milli"),
    (227, "phy_fft_ops", "count"),
    (228, "phy_viterbi_ops", "count"),
    (229, "phy_links", "count"),
    (230, "phy_prb_milli", "milli"),
    (231, "phy_reserved", "exact"),
]

# 64-bit 复合字段：(低字下标, 名称, 容差类别)
BITS = [
    (11, "nr_dl_bits", "bits"),
    (19, "nr_ul_bits", "bits"),
    (29, "lte_bits", "bits"),
    (33, "wifi_bits", "bits"),
    (218, "phy_bits", "bits"),
]

# 每 UE 字段：(偏移, 名称, 类别, 位宽)
# 56 = WL_R_UE_BASE；10 = WL_UE_STRIDE
UE_BASE = 56
UE_STRIDE = 10
UE_FIELDS = [
    (0, "dl_bits", "ue_bits", 64),
    (2, "ul_bits", "ue_bits", 64),
    (4, "dl_queue", "ue_queue", 32),
    (5, "ul_queue", "ue_queue", 32),
    (6, "dl_nack", "ue_count", 32),
    (7, "ul_nack", "ue_count", 32),
    (8, "olla_mdb", "ue_mdb", 32),
    (9, "ul_power_mdb", "ue_mdb", 32),
]

RESULT_WORDS = UE_BASE + 16 * UE_STRIDE + 16   # 216 + 16 个 PHY 字

# 下标 -> 名字（给 --print-names / 调试用）
INDEX_NAMES = {}
for _idx, (_name, _cat) in HEADER.items():
    INDEX_NAMES[_idx] = _name
for _idx, _name, _cat in SCALARS:
    INDEX_NAMES[_idx] = _name
for _lo, _name, _cat in BITS:
    INDEX_NAMES[_lo] = _name + "_lo"
    INDEX_NAMES[_lo + 1] = _name + "_hi"
for _u in range(16):
    for _off, _name, _cat, _width in UE_FIELDS:
        _idx = UE_BASE + _u * UE_STRIDE + _off
        INDEX_NAMES[_idx] = f"ue{_u}_{_name}"
        if _off in (0, 2):
            INDEX_NAMES[_idx + 1] = f"ue{_u}_{_name}_hi"



def _u32(v):
    return int(v) & 0xFFFFFFFF


def _s32(v):
    v = _u32(v)
    return v - 0x100000000 if v & 0x80000000 else v


def named_values(words):
    """把 index->value 的字数组展开成 name->value，并做复合/有符号还原。"""
    out = {}
    for idx, (name, _cat) in HEADER.items():
        if idx in words:
            out[name] = _u32(words[idx])
    for idx, name, cat in SCALARS:
        if idx not in words:
            continue
        v = _u32(words[idx])
        if cat in ("ppm_signed", "mdb"):
            v = _s32(v)
        out[name] = v
    for lo, name, cat in BITS:
        if lo in words and lo + 1 in words:
            out[name] = _u32(words[lo]) | (_u32(words[lo + 1]) << 32)
    for u in range(16):
        base = UE_BASE + u * UE_STRIDE
        for off, name, cat, width in UE_FIELDS:
            idx = base + off
            if off in (0, 2):
                if idx in words and idx + 1 in words:
                    out[f"ue{u}_{name}"] = (_u32(words[idx]) |
                                            (_u32(words[idx + 1]) << 32))
            elif idx in words:
                v = _u32(words[idx])
                if cat == "ue_mdb":
                    v = _s32(v)
                out[f"ue{u}_{name}"] = v
    return out


def tolerance(cat, golden, scale):
    """返回该字段允许的绝对误差；exact 返回 0。"""
    g = abs(golden)
    if cat == "exact":
        return 0
    if cat == "count":
        return max(8.0, g * 0.03) * scale
    if cat == "bits":
        return max(64.0, g * 0.03) * scale
    if cat == "ppm":
        return 10000.0 * scale          # 1 个百分点
    if cat == "ppm_signed":
        return 15000.0 * scale
    if cat == "mslot":
        return max(2.0, g * 0.05) * scale
    if cat == "mdb":
        return 2000.0 * scale           # 2 dB
    if cat == "milli":
        return 100.0 * scale            # 0.1 层 / 0.1 RBG
    if cat == "ue_bits":
        return max(64.0, g * 0.05) * scale
    if cat == "ue_count":
        return max(8.0, g * 0.05) * scale
    if cat == "ue_queue":
        return max(128.0, g * 0.10) * scale
    if cat == "ue_mdb":
        return 3000.0 * scale
    return 0.0
