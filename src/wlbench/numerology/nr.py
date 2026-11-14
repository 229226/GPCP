"""3GPP 38.101-1 transmission bandwidth configuration (NR).

v1 also covers LTE scenarios: the original hll table was shared by both RATs.
Keeping one table here preserves the frozen IR bytes exactly.
"""
N_RB = {
    (20, 15): 106, (50, 15): 270, (100, 15): 273,
    (20, 30): 51, (50, 30): 133, (100, 30): 273,
}

CP_RATIO = 0.0703125
