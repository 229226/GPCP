# 由 tools/gen/gen_cgra_desc.py 从 rtl/cgra/cgra_desc64.yml 生成，不要手改。
"""64B 复合描述符字段表（由 YAML 生成）。"""

# name: (word, lo, width)
FIELDS = {
    "src": (0, 0, 32),
    "spad": (1, 0, 16),
    "page": (1, 16, 1),
    "dir": (1, 17, 1),
    "irq": (1, 18, 1),
    "tgt_rot": (1, 19, 1),
    "in_len": (2, 0, 16),
    "stride": (3, 0, 16),
    "row": (3, 16, 16),
    "cycles": (6, 0, 16),
    "arr_page": (6, 16, 1),
    "arr_en": (6, 31, 1),
    "out_len": (8, 0, 16),
    "out_spad": (8, 16, 16),
    "out_en": (9, 0, 1),
    "out_dst": (10, 0, 32),
    "rep": (12, 0, 16),
    "in_step": (12, 16, 16),
    "out_step": (13, 0, 16),
}


def get(desc, name):
    word, lo, width = FIELDS[name]
    return (desc[word] >> lo) & ((1 << width) - 1)


def put(desc, name, v):
    word, lo, width = FIELDS[name]
    m = ((1 << width) - 1) << lo
    desc[word] = (desc[word] & ~m) | ((v << lo) & m)


def make(**kw):
    desc = [0] * 16
    for k, v in kw.items():
        if k not in FIELDS:
            raise KeyError("未知描述符字段: %s" % k)
        put(desc, k, v)
    return desc
