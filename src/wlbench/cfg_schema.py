"""Protocol parameter schema.

v1 freezes the key set used by the 17 existing scenarios.  New protocols add
keys here (with a new schema_version in the bundle) rather than silently
accepting unknown parameters that no reference model understands.
"""
KNOWN = {
    "a_bits", "bw_mhz", "cp_len", "csi_density", "e_bits", "fft_points",
    "i_ssb", "layers", "l_ra", "m_cs", "n_cs", "n_csi_rs",
    "n_data_subcarriers", "n_hf", "n_id", "n_id1", "n_id2",
    "n_id_1", "n_id_2", "n_rnti", "oversample", "qm", "reg", "rx",
    "scs_khz", "seed", "snr_db", "taps", "nsym", "mcs",
    "ack", "aggregation_level", "arrival", "form", "harq_tx",
    "jobs_per_slot", "length", "preamble", "pss", "reg_per_cce",
    "rv", "slots", "timing_advance", "tp", "util",
    "code_rate", "max_iter", "channel",
}

POSITIVE = {
    "layers", "rx", "qm", "bw_mhz", "scs_khz", "fft_points", "cp_len",
    "n_data_subcarriers", "a_bits", "e_bits", "seed", "max_iter", "slots",
}


def validate(cfg):
    """Reject unknown keys and non-positive values for scale parameters."""
    unknown = sorted(set(cfg) - KNOWN)
    if unknown:
        raise ValueError("unknown cfg parameter(s): %s" % ", ".join(unknown))
    for k, v in cfg.items():
        if k in POSITIVE and (not isinstance(v, (int, float)) or v <= 0):
            raise ValueError("cfg.%s must be positive, got %r" % (k, v))
    return cfg
