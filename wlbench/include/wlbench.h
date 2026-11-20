/* wlbench —— 硬件无关的无线协议整机 benchmark
 *
 * 目标：
 *   1. benchmark 部分是一份纯 C11 程序：整个基站时隙循环（信道、业务、
 *      NR DL/UL 调度、HARQ、PRACH、LTE、WiFi 共存）都在 C 里完成；
 *      不含任何硬件概念、不做任何中间正确性检查，只产出最终功能结果。
 *   2. 结果以固定字数组返回，主机/后端只负责 I/O 和打印。
 *
 * 约束：
 *   - 只用 uint32_t/uint64_t/double，固定容量数组，无动态内存；
 *   - 不使用 libc/libm，数学函数见 wl_math.h；
 *   - 确定性 PCG32，同一 seed 逐值可复现；
 *   - 不依赖字节序、对齐、SIMD、编译器扩展。
 */
#ifndef WLBENCH_H
#define WLBENCH_H

#include <stdint.h>

#include "wl_phy.h"

#define WL_MAX_UE       16
#define WL_MAX_RB       100
#define WL_MAX_TAPS     6
#define WL_MAX_SIN      8
#define WL_DL_HARQ      8
#define WL_UL_HARQ      4
#define WL_NR_MCS       16
#define WL_LTE_MCS      15
#define WL_CQI_DEPTH    8
#define WL_MAX_RBG      100
#define WL_MAX_WIFI     16

#define WL_RESULT_MAGIC 0x574C4243u     /* "WLBC" */
#define WL_SCHEMA       2u

/* ---------------------------------------------------------------- 配置 */
typedef struct {
    int      slots;                 /* 时隙数，1 slot = slot_ms */
    int      n_ue;                  /* UE 数，<= WL_MAX_UE */
    int      seed;
    int      nr_prb;                /* NR 带宽（PRB） */
    int      nr_re_per_prb;         /* 每 PRB 可用 RE */
    int      coreset_rb;
    int      coreset_sym;
    int      rbg_size;
    int      max_layers;
    double   mimo_layer_loss_db;
    int      sinr_map_lin;          /* 0 = EESM, 1 = 线性平均 */
    int      olla;
    double   olla_step_up;
    double   olla_step_down;
    double   olla_max_db;
    double   mismatch_db;
    int      lte_prb;
    int      lte_re_per_prb;
    int      wifi_sta;
    double   wifi_attempt_p;
    int      wifi_shared_band;
    int      dl_full_buffer;
    double   dl_bits_per_slot;
    double   ul_arrival_p;
    double   ul_bits;
    int      ul_harq_rt_slots;
    int      ul_pc;
    double   ul_target_sinr_db;
    double   ul_pc_step_db;
    double   ul_pc_max_db;
    double   ul_loss_db;
    double   delay_spread_ns;
    int      coherence_slots;
    double   speed_kmh;
    double   carrier_ghz;
    double   slot_ms;
    int      cqi_delay_slots;
    int      scenario_mixed;        /* 1 = NR+LTE+WiFi, 0 = 仅 NR */
    wl_phy_cfg phy;                 /* 物理层链路：真的跑 OFDM/FEC */
} wl_config;

/* ---------------------------------------------------------------- 结果字布局
 * 所有结果都是 32-bit 无符号字。浮点量在核心内先转成整数：
 *   ppm   = 百万分之一（速率、比例）
 *   mdb   = 毫 dB
 *   milli = 千分之一（层数、RBG 数）
 *   mslot = 千分之一时隙（时延）
 * 64-bit 比特数拆成 lo/hi 两个字。
 *
 * 测试台与评分器按这些下标读取；改动顺序必须同步 result_spec.py。
 */
enum {
    WL_R_MAGIC = 0,
    WL_R_SCHEMA,
    WL_R_SLOTS,
    WL_R_UES,

    WL_R_NR_DL_TB,
    WL_R_NR_DL_TB_FIRST,
    WL_R_NR_DL_RETX,
    WL_R_NR_DL_NACK,
    WL_R_NR_DL_NACK_FIRST,
    WL_R_NR_RANK2,
    WL_R_NR_LAYERS_SUM,
    WL_R_NR_DL_BITS_LO,
    WL_R_NR_DL_BITS_HI,

    WL_R_NR_UL_TB,
    WL_R_NR_UL_TB_FIRST,
    WL_R_NR_UL_NACK,
    WL_R_NR_UL_NACK_FIRST,
    WL_R_NR_UL_RETX,
    WL_R_NR_UL_DROP,
    WL_R_NR_UL_BITS_LO,
    WL_R_NR_UL_BITS_HI,

    WL_R_NR_PDCCH,
    WL_R_NR_SSB,
    WL_R_NR_SIB1,
    WL_R_NR_PRACH,
    WL_R_NR_RA_OK,
    WL_R_NR_RA_FAIL,

    WL_R_LTE_TB,
    WL_R_LTE_NACK,
    WL_R_LTE_BITS_LO,
    WL_R_LTE_BITS_HI,

    WL_R_WIFI_ATT,
    WL_R_WIFI_COLL,
    WL_R_WIFI_BITS_LO,
    WL_R_WIFI_BITS_HI,

    WL_R_NR_NACK_PPM,
    WL_R_NR_FIRST_BLER_PPM,
    WL_R_NR_EXP_BLER_PPM,
    WL_R_UL_NACK_PPM,
    WL_R_UL_FIRST_BLER_PPM,
    WL_R_UL_EXP_BLER_PPM,
    WL_R_LAT_MEAN_MSLOT,
    WL_R_LAT_MAX_MSLOT,
    WL_R_UL_LAT_MEAN_MSLOT,
    WL_R_JAIN_PPM,
    WL_R_FS_GAIN_PPM,
    WL_R_CCE_UTIL_PPM,
    WL_R_CCE_BLOCKED,
    WL_R_OLLA_MEAN_MDB,
    WL_R_UL_POWER_MEAN_MDB,
    WL_R_UL_SINR_MEAN_MDB,
    WL_R_LAYERS_MEAN_MILLI,
    WL_R_RBG_PER_TB_MILLI,
    WL_R_SPREAD_MDB,
    WL_R_RBG_SPREAD_MDB,
    WL_R_CONFIG_HASH,

    WL_R_UE_BASE
};

#define WL_UE_STRIDE 10
enum {
    WL_UE_DL_BITS_LO = 0,
    WL_UE_DL_BITS_HI,
    WL_UE_UL_BITS_LO,
    WL_UE_UL_BITS_HI,
    WL_UE_DL_QUEUE,
    WL_UE_UL_QUEUE,
    WL_UE_DL_NACK,
    WL_UE_UL_NACK,
    WL_UE_OLLA_MDB,
    WL_UE_UL_POWER_MDB
};

enum {
    WL_R_END_LEGACY = WL_R_UE_BASE + WL_MAX_UE * WL_UE_STRIDE
};

/* ---------------------------------------------------------------- PHY 结果块
 * 追加在 UE 区之后：0..215 的下标与 schema 1 完全一致，只在尾部扩展。
 */
enum {
    WL_R_PHY_TB_OK = WL_R_END_LEGACY,
    WL_R_PHY_TB_FAIL,
    WL_R_PHY_BITS_LO,
    WL_R_PHY_BITS_HI,
    WL_R_PHY_BLER_PPM,
    WL_R_PHY_RAW_BER_PPM,
    WL_R_PHY_POST_BER_PPM,
    WL_R_PHY_EVM_MDB,
    WL_R_PHY_NMSE_MDB,
    WL_R_PHY_SNR_EST_MDB,
    WL_R_PHY_RATE_MILLI,
    WL_R_PHY_FFT_OPS,
    WL_R_PHY_VITERBI_OPS,
    WL_R_PHY_LINKS,
    WL_R_PHY_PRB_MILLI,
    WL_R_PHY_RESERVED,

    WL_R_END
};

#define WL_RESULT_WORDS WL_R_END

/* ---------------------------------------------------------------- 确定性 RNG */
typedef struct {
    uint64_t state;
    uint64_t inc;
    double   spare;
    int      has_spare;
} wl_rng;

/* ---------------------------------------------------------------- 复数 */
typedef struct {
    double re;
    double im;
} wl_cplx;

/* ---------------------------------------------------------------- 链路表 */
typedef struct {
    int    mcs;
    int    qm;
    int    rate;
    double req;
} wl_mcs;

/* ---------------------------------------------------------------- 信道 */
typedef struct {
    int     n_rb;
    int     taps;
    int     n_sin;
    double  snr_db;
    double  sc_hz;
    double  ds_ns;
    double  fd;
    double  slot_s;
    double  t;
    double  delays[WL_MAX_TAPS];
    double  amp[WL_MAX_TAPS];
    wl_cplx phasor[WL_MAX_RB][WL_MAX_TAPS];
    wl_cplx state[WL_MAX_TAPS][WL_MAX_SIN];
    wl_cplx rot[WL_MAX_SIN];
    wl_cplx coef[WL_MAX_TAPS];
    double  gain_db[WL_MAX_RB];
    double  cal_db;
} wl_channel;

/* ---------------------------------------------------------------- HARQ */
typedef struct {
    int    rv;
    int    attempts;
    int    pending_at;
    int    tbs;
    int    rank;
    int    qm;
    int    first_slot;
    double combining_db;
    double req_db;
} wl_harq;

/* ---------------------------------------------------------------- UE */
typedef struct {
    int      uid;
    int      rnti;
    double   snr_db;
    double   dl_queue;
    double   ul_queue;
    double   avg_rate;
    wl_harq  harq[WL_DL_HARQ];
    wl_harq  ul_harq[WL_UL_HARQ];
    double   ul_power_db;
    int      connected;
    int      ra_stage;
    int      ra_try_slot;
    uint64_t dl_bits;
    uint64_t ul_bits;
    int64_t  dl_tbs;
    int64_t  dl_retx;
    int64_t  dl_nack;
    int64_t  ul_nack;
    int64_t  ul_retx;
    double   ul_sinr_sum;
    int64_t  ul_sinr_cnt;
    double   olla_db;
    wl_channel ch;
    double   cqi_gain[WL_MAX_RB];
    double   cqi_hist[WL_CQI_DEPTH][WL_MAX_RB];
    int      cqi_n;
    int      cqi_head;
    int64_t  dl_lat_n;
    double   dl_lat_sum;
    double   dl_lat_max;
    int64_t  ul_lat_n;
    double   ul_lat_sum;
    double   ul_lat_max;
    int      dl_head_slot;
} wl_ue;

/* ---------------------------------------------------------------- WiFi */
typedef struct {
    int      sid;
    double   snr_db;
    uint64_t tx_bits;
    int      backoff;
    int      collisions;
    int      attempts;
} wl_wifi;

/* ---------------------------------------------------------------- KPI */
typedef struct {
    int64_t  nr_dl_tb;
    int64_t  nr_dl_tb_first;
    int64_t  nr_dl_retx;
    int64_t  nr_dl_nack;
    int64_t  nr_dl_nack_first;
    int64_t  nr_rank2;
    int64_t  nr_layers_sum;
    uint64_t nr_dl_bits;

    int64_t  nr_ul_tb;
    int64_t  nr_ul_tb_first;
    int64_t  nr_ul_nack;
    int64_t  nr_ul_nack_first;
    int64_t  nr_ul_retx;
    int64_t  nr_ul_drop;
    uint64_t nr_ul_bits;

    int64_t  nr_pdcch;
    int64_t  nr_ssb;
    int64_t  nr_sib1;
    int64_t  nr_prach;
    int64_t  nr_ra_ok;
    int64_t  nr_ra_fail;

    int64_t  lte_dl_tb;
    int64_t  lte_nack;
    uint64_t lte_dl_bits;

    int64_t  wifi_attempt;
    int64_t  wifi_coll;
    uint64_t wifi_bits;
} wl_kpi;

/* ---------------------------------------------------------------- 全局状态 */
typedef struct {
    wl_config cfg;
    wl_rng    rng;
    int       slot;
    wl_ue     ues[WL_MAX_UE];
    int       n_ue;
    wl_wifi   wifi[WL_MAX_WIFI];
    int       n_wifi;
    wl_kpi    kpi;

    int       cce_cap;
    int       cce_used;
    int       cce_blocked;
    int       ul_rr;
    int64_t   rbg_alloc_sum;
    int64_t   cce_sum;

    double    fs_bits;
    double    flat_bits;
    double    nr_bler_sum;
    int64_t   nr_tb_cnt;
    double    ul_bler_sum;

    wl_phy_ctx phy_ctx;
} wl_state;

/* ---------------------------------------------------------------- API */
void wl_default_config(wl_config *c);
void wl_init(wl_state *s, const wl_config *c);

/* 推进恰好一个时隙（1 slot = cfg.slot_ms）。 */
void wl_slot(wl_state *s);

/* 从当前 slot 跑到 cfg.slots；主机只调用它 + 打印。 */
void wl_run(wl_state *s);

/* 把最终功能结果打包成固定字数组，长度 = WL_RESULT_WORDS。 */
void wl_result(const wl_state *s, uint32_t *words);

/* 供主机申报字节数（也用于静态检查）。 */
int wl_result_words(void);

#endif /* WLBENCH_H */
