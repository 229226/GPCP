/* wl_phy —— CP-OFDM 物理层链路（纯 C11，无 libc/libm、无动态内存）
 *
 * 这一层真的做基带运算，不是查表：
 *   CRC24A -> 卷积编码 -> 速率匹配 -> 加扰 -> QAM 映射 -> 资源映射
 *   -> IFFT + CP -> TDL 多径 + AWGN -> 去 CP + FFT
 *   -> DMRS 最小二乘信道估计（可选 DFT 去噪）-> MMSE 均衡
 *   -> max-log LLR -> 解扰 -> 解速率匹配 -> Viterbi -> CRC 校验
 *
 * 全程只依赖 <stdint.h> 与 wl_math.h，同一份代码可在主机与裸机上跑；
 * 随机数、量化、浮点顺序都是确定的，同 seed 逐比特可复现。
 */
#ifndef WLBENCH_PHY_H
#define WLBENCH_PHY_H

#include <stdint.h>

/* ---------------------------------------------------------------- 容量上限
 * 链路带宽按 target_coded 自动选，实际 PRB 数不超过 WL_PHY_MAX_PRB，
 * 因此下面的缓冲区是编译期确定的，不需要 malloc。
 */
#define WL_PHY_MAX_PRB      8
#define WL_PHY_MAX_SC       (WL_PHY_MAX_PRB * 12)   /* 96 个子载波 */
#define WL_PHY_MAX_FFT      256
#define WL_PHY_MAX_SYM      14
#define WL_PHY_MAX_CODED    8192                    /* 速率匹配后的比特 */
#define WL_PHY_MAX_ENC      9216                    /* 编码器原始输出 */
#define WL_PHY_MAX_INFO     4096
#define WL_PHY_TAPS         3

typedef struct {
    double re;
    double im;
} wl_ph_c;

/* ---------------------------------------------------------------- 配置 */
typedef struct {
    int    enable;        /* 0 = 只算统计 BLER（旧行为），1 = 跑真实链路 */
    int    max_prb;       /* 单次链路的 PRB 上限，<= WL_PHY_MAX_PRB */
    int    target_coded;  /* 每次链路的目标编码比特数，决定带宽 */
    int    n_sym;         /* 每时隙 OFDM 符号数，<= WL_PHY_MAX_SYM */
    int    ce_mode;       /* 0 = LS + 线性插值，1 = 追加 DFT 截断去噪 */
    double noise_fig_db;  /* 接收机噪声系数，等效降低 SNR */
    double ds_ns;         /* 时延扩展，决定多径间隔 */
    double speed_kmh;     /* 多普勒 */
    double fc_ghz;        /* 载频 */
    double scs_khz;       /* 子载波间隔，决定采样率 */
} wl_phy_cfg;

/* ---------------------------------------------------------------- 统计 */
typedef struct {
    int64_t  tb_ok;
    int64_t  tb_fail;
    uint64_t bits_ok;
    int64_t  raw_err;      /* 解调硬判后的误比特（编码比特，pre-FEC） */
    int64_t  raw_bits;
    int64_t  dec_err;      /* 译码后的误比特（信息比特，post-FEC） */
    int64_t  dec_bits;
    double   evm_acc;      /* 判决导向 EVM，线性幅度 */
    int64_t  evm_n;
    double   nmse_acc;     /* 信道估计归一化均方误差，线性 */
    int64_t  nmse_n;
    double   snr_acc;      /* 由 DMRS 残差估出的 SNR，dB */
    int64_t  snr_n;
    double   rate_acc;     /* 实际码率 */
    int64_t  rate_n;
    int64_t  fft_ops;      /* 累计蝶形数 */
    int64_t  viterbi_ops;  /* 累计网格步数 × 状态数 */
    int64_t  prb_sum;      /* 累计链路带宽，用于求均值 */
    double   sigma2;       /* 最近一次链路的噪声估计（内部用） */
} wl_phy_stats;

/* ---------------------------------------------------------------- 上下文 */
typedef struct {
    wl_ph_c tx[WL_PHY_MAX_FFT];
    wl_ph_c rx[WL_PHY_MAX_FFT];
    wl_ph_c td[WL_PHY_MAX_FFT + 64];
    wl_ph_c grid[WL_PHY_MAX_SYM][WL_PHY_MAX_SC];
    wl_ph_c h_ir[WL_PHY_MAX_FFT];          /* 时域冲激响应 */
    wl_ph_c h_true[WL_PHY_MAX_FFT];        /* 真实频响（只给指标用） */
    wl_ph_c h_ls[WL_PHY_MAX_SC];
    wl_ph_c h_est[WL_PHY_MAX_SC];
    wl_ph_c eq[WL_PHY_MAX_SC];

    uint8_t info[WL_PHY_MAX_INFO];
    uint8_t enc[WL_PHY_MAX_ENC];
    uint8_t coded[WL_PHY_MAX_CODED];
    uint8_t rx_info[WL_PHY_MAX_INFO + 16];

    int16_t llr[WL_PHY_MAX_CODED];
    int16_t llr_dec[WL_PHY_MAX_ENC];
    uint64_t dec[WL_PHY_MAX_INFO + 16];

    wl_phy_stats st;
} wl_phy_ctx;

/* ---------------------------------------------------------------- API */
void wl_phy_init(wl_phy_ctx *p, const wl_phy_cfg *c);

/* 跑一条完整链路。返回 1 = CRC 通过（ACK），0 = 失败（NACK）。
 * snr_db 是每 RE 的 SNR；qm ∈ {2,4,6,8}；rate 是码率（0..1）。
 * 带宽由 cfg->target_coded / qm 反推，内部限幅到 cfg->max_prb。 */
int wl_phy_link(wl_phy_ctx *p, const wl_phy_cfg *c, double snr_db,
                int qm, double rate, uint64_t seed);

/* 供自检使用的原语 */
void wl_phy_fft(wl_ph_c *x, int n, int inverse);
void wl_phy_map(const uint8_t *bits, int nbits, int qm, wl_ph_c *out);
void wl_phy_demap(const wl_ph_c *sym, int nsym, int qm, double sigma2,
                  int16_t *llr);
void wl_phy_slice(wl_ph_c *v, int qm);

#endif /* WLBENCH_PHY_H */
