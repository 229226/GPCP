/* wlbench 整机协议核心（纯 C11，无 libc/libm、无动态内存）
 *
 * 一个 wl_slot() 推进 1 个时隙：
 *   信道推进 -> CQI 存史 -> 业务到达 -> 空口仲裁(WiFi/LBT) ->
 *   SSB/SIB1 -> NR DL 调度 -> NR UL 调度 -> PRACH -> LTE
 *
 * benchmark 语义：不做任何中间正确性检查，只维护协议状态并累计 KPI；
 * 最终结果由 wl_result() 打包成固定字数组。
 */
#include "wlbench.h"
#include "wl_math.h"

/* ---------------------------------------------------------------- 基础工具 */
static void wl_memzero(void *p, uint64_t n)
{
    unsigned char *b = (unsigned char *)p;
    uint64_t i;
    for (i = 0; i < n; i++) b[i] = 0;
}

static wl_cplx wl_mkc(double re, double im)
{
    wl_cplx z;
    z.re = re;
    z.im = im;
    return z;
}

static wl_cplx wl_cadd(wl_cplx a, wl_cplx b)
{
    return wl_mkc(a.re + b.re, a.im + b.im);
}

static wl_cplx wl_cmul(wl_cplx a, wl_cplx b)
{
    return wl_mkc(a.re * b.re - a.im * b.im,
                  a.re * b.im + a.im * b.re);
}

static double wl_cabs2(wl_cplx a)
{
    return a.re * a.re + a.im * a.im;
}

/* ---------------------------------------------------------------- PCG32 */
static void wl_rng_seed(wl_rng *r, uint64_t seed)
{
    r->state = 0;
    r->inc = (seed << 1u) | 1u;
    r->has_spare = 0;
    r->spare = 0.0;
    {
        uint64_t old = r->state;
        r->state = old * 6364136223846793005ULL + r->inc;
    }
    r->state += seed;
    {
        uint64_t old = r->state;
        r->state = old * 6364136223846793005ULL + r->inc;
    }
}

static uint32_t wl_rng_u32(wl_rng *r)
{
    uint64_t old = r->state;
    uint32_t xs;
    uint32_t rot;
    r->state = old * 6364136223846793005ULL + r->inc;
    xs = (uint32_t)(((old >> 18u) ^ old) >> 27u);
    rot = (uint32_t)(old >> 59u);
    return (xs >> rot) | (xs << ((-rot) & 31u));
}

static double wl_rng_random(wl_rng *r)
{
    return wl_rng_u32(r) * (1.0 / 4294967296.0);
}

static double wl_rng_uniform(wl_rng *r, double lo, double hi)
{
    return lo + (hi - lo) * wl_rng_random(r);
}

static int wl_rng_randrange(wl_rng *r, int hi)
{
    if (hi <= 0) return 0;
    return (int)(wl_rng_u32(r) % (uint32_t)hi);
}

static int wl_rng_randint(wl_rng *r, int lo, int hi)
{
    return lo + wl_rng_randrange(r, hi - lo + 1);
}

/* ---------------------------------------------------------------- 链路表 */
static wl_mcs g_nr_table[WL_NR_MCS] = {
    {0, 2, 120, 0}, {1, 2, 157, 0}, {3, 2, 193, 0}, {5, 2, 308, 0},
    {7, 2, 449, 0}, {9, 2, 602, 0}, {11, 4, 378, 0}, {13, 4, 490, 0},
    {15, 4, 616, 0}, {17, 6, 438, 0}, {20, 6, 553, 0}, {22, 6, 650, 0},
    {24, 6, 753, 0}, {26, 6, 873, 0}, {27, 8, 873, 0}, {28, 8, 948, 0}
};

static wl_mcs g_lte_table[WL_LTE_MCS] = {
    {0, 2, 120, 0}, {2, 2, 193, 0}, {4, 2, 308, 0}, {6, 2, 449, 0},
    {8, 2, 602, 0}, {10, 4, 378, 0}, {12, 4, 490, 0}, {14, 4, 616, 0},
    {16, 4, 616, 0}, {18, 6, 438, 0}, {20, 6, 553, 0}, {22, 6, 650, 0},
    {24, 6, 753, 0}, {26, 6, 873, 0}, {28, 6, 948, 0}
};

static const double g_eesm_beta[4] = {2.0, 4.0, 8.0, 12.0};

static int wl_qm_index(int qm)
{
    if (qm == 2) return 0;
    if (qm == 4) return 1;
    if (qm == 6) return 2;
    return 3;
}

static double wl_required_sinr(int qm, int rate)
{
    double se = (double)qm * (double)rate / 1024.0;
    double x = wl_exp2(se) - 1.0;
    if (x < 1e-3) x = 1e-3;
    return 10.0 * wl_log10(x) + 2.0;
}

static void wl_link_init(void)
{
    int i;
    for (i = 0; i < WL_NR_MCS; i++)
        g_nr_table[i].req = wl_required_sinr(g_nr_table[i].qm, g_nr_table[i].rate);
    for (i = 0; i < WL_LTE_MCS; i++)
        g_lte_table[i].req = wl_required_sinr(g_lte_table[i].qm, g_lte_table[i].rate);
}

static double wl_bler_req(double sinr_db, double req_db)
{
    return 1.0 / (1.0 + wl_exp((sinr_db - req_db) / 0.6));
}

static double wl_bler_of(double sinr_db, int qm, int rate)
{
    return wl_bler_req(sinr_db, wl_required_sinr(qm, rate));
}

static const wl_mcs *wl_pick_mcs(const wl_mcs *tab, int n, double sinr_db)
{
    const wl_mcs *best = &tab[0];
    int i;
    for (i = 0; i < n; i++)
        if (tab[i].req <= sinr_db - 1.0) best = &tab[i];
    return best;
}

static const wl_mcs *wl_pick_mcs_eff(const wl_mcs *tab, int n, const double eff[4])
{
    const wl_mcs *best = &tab[0];
    int i;
    for (i = 0; i < n; i++) {
        int k = wl_qm_index(tab[i].qm);
        if (tab[i].req <= eff[k] - 1.0) best = &tab[i];
    }
    return best;
}

static int wl_cce_level(double sinr_db)
{
    if (sinr_db >= 10.0) return 1;
    if (sinr_db >= 4.0) return 2;
    if (sinr_db >= -2.0) return 4;
    return 8;
}

static double wl_lin_db(const double *v, int n)
{
    double acc = 0.0;
    int i;
    if (n <= 0) return -99.0;
    for (i = 0; i < n; i++) acc += wl_pow10(v[i] / 10.0);
    acc /= (double)n;
    return 10.0 * wl_log10(acc > 1e-12 ? acc : 1e-12);
}

static double wl_eesm_db(const double *v, int n, double beta)
{
    double acc = 0.0;
    int i;
    if (n <= 0) return -99.0;
    for (i = 0; i < n; i++) acc += wl_exp(-(wl_pow10(v[i] / 10.0)) / beta);
    acc /= (double)n;
    return 10.0 * wl_log10(-beta * wl_log(acc > 1e-12 ? acc : 1e-12));
}

/* ---------------------------------------------------------------- 信道 */
static void wl_ch_calc(wl_channel *ch, int normalize)
{
    int rb, l;
    double mean = 0.0;

    for (rb = 0; rb < ch->n_rb; rb++) {
        wl_cplx acc = wl_mkc(0.0, 0.0);
        double p;
        for (l = 0; l < ch->taps; l++)
            acc = wl_cadd(acc, wl_cmul(wl_mkc(ch->amp[l], 0.0),
                                       wl_cmul(ch->coef[l], ch->phasor[rb][l])));
        p = wl_cabs2(acc);
        ch->gain_db[rb] = p > 1e-4 ? p : 1e-4;
        mean += ch->gain_db[rb];
    }
    mean /= (double)ch->n_rb;

    for (rb = 0; rb < ch->n_rb; rb++) {
        double v = ch->gain_db[rb];
        if (normalize) v /= (mean > 0.0 ? mean : 1.0);
        v = v > 1e-4 ? v : 1e-4;
        ch->gain_db[rb] = 10.0 * wl_log10(v) - (normalize ? 0.0 : ch->cal_db);
    }
}

static void wl_ch_realize(wl_channel *ch, wl_rng *rng)
{
    int l;
    for (l = 0; l < ch->taps; l++) {
        double ph = wl_rng_uniform(rng, 0.0, 2.0 * WL_PI);
        ch->coef[l] = wl_mkc(wl_cos(ph), wl_sin(ph));
    }
    wl_ch_calc(ch, 1);
}

static void wl_ch_init(wl_channel *ch, wl_rng *rng, int n_rb, double snr_db,
                       double sc_hz, double ds_ns, double doppler_hz, double slot_s)
{
    int l, n, rb;
    double sum = 0.0, m = 0.0;

    wl_memzero(ch, sizeof(*ch));
    if (n_rb > WL_MAX_RB) n_rb = WL_MAX_RB;
    if (n_rb < 1) n_rb = 1;

    ch->n_rb = n_rb;
    ch->taps = WL_MAX_TAPS;
    ch->n_sin = WL_MAX_SIN;
    ch->snr_db = snr_db;
    ch->sc_hz = sc_hz;
    ch->ds_ns = ds_ns;
    ch->fd = doppler_hz;
    ch->slot_s = slot_s;

    for (l = 0; l < ch->taps; l++) {
        ch->delays[l] = (double)l * (2.5 * ds_ns) / (double)(ch->taps - 1);
        ch->amp[l] = wl_exp(-ch->delays[l] / ds_ns);
        sum += ch->amp[l] * ch->amp[l];
    }
    if (sum <= 0.0) sum = 1.0;
    for (l = 0; l < ch->taps; l++) ch->amp[l] /= wl_sqrt(sum);

    for (rb = 0; rb < n_rb; rb++) {
        for (l = 0; l < ch->taps; l++) {
            double ph = -2.0 * WL_PI * (((double)rb - (double)n_rb / 2.0) * sc_hz) *
                        (ch->delays[l] * 1e-9);
            ch->phasor[rb][l] = wl_mkc(wl_cos(ph), wl_sin(ph));
        }
    }

    if (doppler_hz > 0.0) {
        for (n = 0; n < ch->n_sin; n++) {
            double ph = 2.0 * WL_PI * doppler_hz *
                        wl_cos(2.0 * WL_PI * (double)n / (double)ch->n_sin) * slot_s;
            ch->rot[n] = wl_mkc(wl_cos(ph), wl_sin(ph));
        }
        for (l = 0; l < ch->taps; l++) {
            for (n = 0; n < ch->n_sin; n++) {
                double a = wl_rng_uniform(rng, 0.0, 2.0 * WL_PI);
                ch->state[l][n] = wl_mkc(wl_cos(a), wl_sin(a));
            }
        }
        for (l = 0; l < ch->taps; l++) {
            wl_cplx acc = wl_mkc(0.0, 0.0);
            for (n = 0; n < ch->n_sin; n++) acc = wl_cadd(acc, ch->state[l][n]);
            ch->coef[l] = wl_mkc(acc.re / wl_sqrt((double)ch->n_sin),
                                 acc.im / wl_sqrt((double)ch->n_sin));
        }
        for (rb = 0; rb < n_rb; rb++) {
            wl_cplx acc = wl_mkc(0.0, 0.0);
            double p;
            for (l = 0; l < ch->taps; l++)
                acc = wl_cadd(acc, wl_cmul(wl_mkc(ch->amp[l], 0.0),
                                           wl_cmul(ch->coef[l], ch->phasor[rb][l])));
            p = wl_cabs2(acc);
            ch->gain_db[rb] = 10.0 * wl_log10(p > 1e-4 ? p : 1e-4);
        }
        for (rb = 0; rb < n_rb; rb++) m += wl_pow10(ch->gain_db[rb] / 10.0);
        m /= (double)n_rb;
        ch->cal_db = 10.0 * wl_log10(m > 1e-6 ? m : 1e-6);
        for (rb = 0; rb < n_rb; rb++) ch->gain_db[rb] -= ch->cal_db;
    } else {
        for (l = 0; l < ch->taps; l++) ch->coef[l] = wl_mkc(1.0, 0.0);
        wl_ch_realize(ch, rng);
    }
}

static void wl_ch_advance(wl_channel *ch)
{
    int l, n;
    if (ch->fd <= 0.0) return;
    ch->t += ch->slot_s;
    for (l = 0; l < ch->taps; l++)
        for (n = 0; n < ch->n_sin; n++)
            ch->state[l][n] = wl_cmul(ch->state[l][n], ch->rot[n]);
    for (l = 0; l < ch->taps; l++) {
        wl_cplx acc = wl_mkc(0.0, 0.0);
        for (n = 0; n < ch->n_sin; n++) acc = wl_cadd(acc, ch->state[l][n]);
        ch->coef[l] = wl_mkc(acc.re / wl_sqrt((double)ch->n_sin),
                             acc.im / wl_sqrt((double)ch->n_sin));
    }
    wl_ch_calc(ch, 0);
}

static double wl_ch_sinr_rb(const wl_channel *ch, int rb, double penalty_db)
{
    int i = rb % ch->n_rb;
    if (i < 0) i += ch->n_rb;
    return ch->snr_db + ch->gain_db[i] - penalty_db;
}

static double wl_ch_spread_db(const wl_channel *ch)
{
    double lo = ch->gain_db[0];
    double hi = ch->gain_db[0];
    int i;
    for (i = 1; i < ch->n_rb; i++) {
        if (ch->gain_db[i] < lo) lo = ch->gain_db[i];
        if (ch->gain_db[i] > hi) hi = ch->gain_db[i];
    }
    return hi - lo;
}

static double wl_ch_rbg_spread_db(const wl_channel *ch, int rbg_size)
{
    double v[WL_MAX_RB];
    double m = 0.0, sd = 0.0;
    int i, k, n = 0;
    if (rbg_size < 1) rbg_size = 1;
    for (i = 0; i < ch->n_rb; i += rbg_size) {
        int hi = i + rbg_size < ch->n_rb ? i + rbg_size : ch->n_rb;
        double a = 0.0;
        for (k = i; k < hi; k++) a += ch->gain_db[k];
        v[n++] = a / (double)(hi - i);
    }
    if (n <= 0) return 0.0;
    for (i = 0; i < n; i++) m += v[i];
    m /= (double)n;
    for (i = 0; i < n; i++) {
        double d = v[i] - m;
        sd += d * d;
    }
    return wl_sqrt(sd / (double)n);
}

/* ---------------------------------------------------------------- 调度工具 */
static double wl_doppler_hz(const wl_state *s)
{
    return s->cfg.speed_kmh / 3.6 * (s->cfg.carrier_ghz * 1e9) / 3e8;
}

static int wl_take_cce(wl_state *s, int level)
{
    if (s->cce_used + level > s->cce_cap) {
        s->cce_blocked++;
        return 0;
    }
    s->cce_used += level;
    return 1;
}

static void wl_olla_update(wl_state *s, wl_ue *u, int ack)
{
    double d;
    if (!s->cfg.olla) return;
    d = ack ? s->cfg.olla_step_up : -s->cfg.olla_step_down;
    u->olla_db += d;
    if (u->olla_db > s->cfg.olla_max_db) u->olla_db = s->cfg.olla_max_db;
    if (u->olla_db < -s->cfg.olla_max_db) u->olla_db = -s->cfg.olla_max_db;
}

static void wl_lat_add(int64_t *n, double *sum, double *mx, double v)
{
    *n += 1;
    *sum += v;
    if (v > *mx) *mx = v;
}

static void wl_eff_sinr(const wl_state *s, const wl_ue *u, const int *rbs, int n,
                        double penalty, double extra, const double *gain_db,
                        double out[4])
{
    double vals[WL_MAX_RB];
    int i, k;
    if (n > WL_MAX_RB) n = WL_MAX_RB;
    if (n < 1) {
        for (k = 0; k < 4; k++) out[k] = -99.0;
        return;
    }
    for (i = 0; i < n; i++) {
        if (gain_db) {
            int rb = rbs[i] % u->ch.n_rb;
            vals[i] = u->snr_db + gain_db[rb] - penalty + extra;
        } else {
            vals[i] = wl_ch_sinr_rb(&u->ch, rbs[i], penalty) + extra;
        }
    }
    if (s->cfg.sinr_map_lin) {
        double v = wl_lin_db(vals, n);
        for (k = 0; k < 4; k++) out[k] = v;
    } else {
        for (k = 0; k < 4; k++) out[k] = wl_eesm_db(vals, n, g_eesm_beta[k]);
    }
}

static void wl_amc_eff(const wl_state *s, const wl_ue *u, const double in[4], double out[4])
{
    double db = s->cfg.olla ? u->olla_db : 0.0;
    int k;
    for (k = 0; k < 4; k++) out[k] = in[k] + db;
}

static void wl_layer_sinr(const wl_state *s, const double in[4], int rank, double out[4])
{
    double pen = rank > 1 ? 10.0 * wl_log10((double)rank) + s->cfg.mimo_layer_loss_db : 0.0;
    int k;
    for (k = 0; k < 4; k++) out[k] = in[k] - pen;
}

static int wl_pick_rank(const wl_state *s, const double eff[4])
{
    const wl_mcs *m1;
    const wl_mcs *m2;
    double b1, g1, e2[4], m2b, g2;

    m1 = wl_pick_mcs_eff(g_nr_table, WL_NR_MCS, eff);
    b1 = wl_bler_req(eff[wl_qm_index(m1->qm)] - s->cfg.mismatch_db, m1->req);
    g1 = (double)m1->qm * (double)m1->rate * (1.0 - b1);
    if (s->cfg.max_layers < 2) return 1;

    wl_layer_sinr(s, eff, 2, e2);
    m2 = wl_pick_mcs_eff(g_nr_table, WL_NR_MCS, e2);
    m2b = wl_bler_req(e2[wl_qm_index(m2->qm)] - s->cfg.mismatch_db, m2->req);
    g2 = 2.0 * (double)m2->qm * (double)m2->rate * (1.0 - m2b) * (1.0 - m2b);
    return g2 > g1 ? 2 : 1;
}

static int wl_ue_dl_due(const wl_ue *u, int slot)
{
    int i;
    for (i = 0; i < WL_DL_HARQ; i++)
        if (u->harq[i].pending_at >= 0 && u->harq[i].pending_at <= slot) return 1;
    return 0;
}

/* ---------------------------------------------------------------- 默认配置 */
void wl_default_config(wl_config *c)
{
    wl_memzero(c, sizeof(*c));
    c->slots = 500;
    c->n_ue = 8;
    c->seed = 1;
    c->nr_prb = 51;
    c->nr_re_per_prb = 12 * 14 - 12;
    c->coreset_rb = 24;
    c->coreset_sym = 3;
    c->rbg_size = 4;
    c->max_layers = 2;
    c->mimo_layer_loss_db = 3.0;
    c->sinr_map_lin = 0;
    c->olla = 1;
    c->olla_step_up = 0.05;
    c->olla_step_down = 0.45;
    c->olla_max_db = 6.0;
    c->mismatch_db = 0.0;
    c->lte_prb = 50;
    c->lte_re_per_prb = 12 * 14 - 12;
    c->wifi_sta = 4;
    c->wifi_attempt_p = 0.55;
    c->wifi_shared_band = 0;
    c->dl_full_buffer = 1;
    c->dl_bits_per_slot = 4000.0;
    c->ul_arrival_p = 0.10;
    c->ul_bits = 1200.0;
    c->ul_harq_rt_slots = 3;
    c->ul_pc = 1;
    c->ul_target_sinr_db = 10.0;
    c->ul_pc_step_db = 0.5;
    c->ul_pc_max_db = 10.0;
    c->ul_loss_db = 0.0;
    c->delay_spread_ns = 500.0;
    c->coherence_slots = 20;
    c->speed_kmh = 3.0;
    c->carrier_ghz = 3.5;
    c->slot_ms = 1.0;
    c->cqi_delay_slots = 4;
    c->scenario_mixed = 1;

    c->phy.enable = 1;
    c->phy.max_prb = 6;
    c->phy.target_coded = 1024;
    c->phy.n_sym = 14;
    c->phy.ce_mode = 1;
    c->phy.noise_fig_db = 0.0;
    c->phy.ds_ns = 500.0;
    c->phy.speed_kmh = 3.0;
    c->phy.fc_ghz = 3.5;
    c->phy.scs_khz = 15.0;
}

/* ---------------------------------------------------------------- 初始化 */
void wl_init(wl_state *s, const wl_config *c)
{
    int i, k;

    wl_memzero(s, sizeof(*s));
    s->cfg = *c;

    if (s->cfg.slots < 0) s->cfg.slots = 0;
    if (s->cfg.n_ue < 1) s->cfg.n_ue = 1;
    if (s->cfg.n_ue > WL_MAX_UE) s->cfg.n_ue = WL_MAX_UE;
    if (s->cfg.nr_prb < 1) s->cfg.nr_prb = 1;
    if (s->cfg.nr_prb > WL_MAX_RB) s->cfg.nr_prb = WL_MAX_RB;
    if (s->cfg.nr_re_per_prb < 1) s->cfg.nr_re_per_prb = 1;
    if (s->cfg.coreset_rb < 1) s->cfg.coreset_rb = 1;
    if (s->cfg.coreset_sym < 1) s->cfg.coreset_sym = 1;
    if (s->cfg.rbg_size < 1) s->cfg.rbg_size = 1;
    if (s->cfg.rbg_size > s->cfg.nr_prb) s->cfg.rbg_size = s->cfg.nr_prb;
    if (s->cfg.max_layers < 1) s->cfg.max_layers = 1;
    if (s->cfg.max_layers > 2) s->cfg.max_layers = 2;
    if (s->cfg.lte_prb < 1) s->cfg.lte_prb = 1;
    if (s->cfg.lte_prb > WL_MAX_RB) s->cfg.lte_prb = WL_MAX_RB;
    if (s->cfg.lte_re_per_prb < 1) s->cfg.lte_re_per_prb = 1;
    if (s->cfg.wifi_sta < 0) s->cfg.wifi_sta = 0;
    if (s->cfg.wifi_sta > WL_MAX_WIFI) s->cfg.wifi_sta = WL_MAX_WIFI;
    if (s->cfg.cqi_delay_slots < 1) s->cfg.cqi_delay_slots = 1;
    if (s->cfg.cqi_delay_slots > WL_CQI_DEPTH) s->cfg.cqi_delay_slots = WL_CQI_DEPTH;
    if (s->cfg.coherence_slots < 1) s->cfg.coherence_slots = 1;
    if (s->cfg.slot_ms <= 0.0) s->cfg.slot_ms = 1.0;
    if (s->cfg.delay_spread_ns <= 0.0) s->cfg.delay_spread_ns = 1.0;
    if (s->cfg.ul_harq_rt_slots < 0) s->cfg.ul_harq_rt_slots = 0;

    wl_link_init();
    wl_rng_seed(&s->rng, (uint64_t)s->cfg.seed);

    s->n_ue = s->cfg.n_ue;
    s->cce_cap = s->cfg.coreset_rb * s->cfg.coreset_sym / 6;
    if (s->cce_cap < 1) s->cce_cap = 1;

    for (i = 0; i < s->n_ue; i++) {
        wl_ue *u = &s->ues[i];
        double frac = (double)i / (double)(s->n_ue > 1 ? s->n_ue - 1 : 1);

        wl_memzero(u, sizeof(*u));
        u->uid = i;
        u->rnti = 0x4601 + i;
        u->snr_db = 22.0 - 28.0 * frac + wl_rng_uniform(&s->rng, -1.5, 1.5);
        u->avg_rate = 1.0;
        u->connected = 1;
        u->ra_try_slot = -1;
        u->dl_head_slot = -1;
        for (k = 0; k < WL_DL_HARQ; k++) u->harq[k].pending_at = -1;
        for (k = 0; k < WL_UL_HARQ; k++) {
            u->ul_harq[k].pending_at = -1;
            u->ul_harq[k].first_slot = -1;
        }
        wl_ch_init(&u->ch, &s->rng, s->cfg.nr_prb, u->snr_db, 15e3 * 12.0,
                   s->cfg.delay_spread_ns, wl_doppler_hz(s), s->cfg.slot_ms * 1e-3);
    }

    s->n_wifi = s->cfg.wifi_sta;
    for (i = 0; i < s->n_wifi; i++) {
        s->wifi[i].sid = i;
        s->wifi[i].snr_db = wl_rng_uniform(&s->rng, 2.0, 20.0);
        s->wifi[i].backoff = 0;
        s->wifi[i].tx_bits = 0;
        s->wifi[i].attempts = 0;
        s->wifi[i].collisions = 0;
    }

    /* 物理层：信道参数跟随全局配置 */
    s->cfg.phy.ds_ns = s->cfg.delay_spread_ns;
    s->cfg.phy.speed_kmh = s->cfg.speed_kmh;
    s->cfg.phy.fc_ghz = s->cfg.carrier_ghz;
    if (s->cfg.phy.max_prb < 1) s->cfg.phy.max_prb = 1;
    if (s->cfg.phy.max_prb > WL_PHY_MAX_PRB) s->cfg.phy.max_prb = WL_PHY_MAX_PRB;
    if (s->cfg.phy.target_coded < 128) s->cfg.phy.target_coded = 128;
    if (s->cfg.phy.scs_khz <= 0.0) s->cfg.phy.scs_khz = 15.0;
    wl_phy_init(&s->phy_ctx, &s->cfg.phy);
}

/* ---------------------------------------------------------------- 广播 */
static void wl_broadcast(wl_state *s)
{
    if ((s->slot % 20) == 0) {
        s->kpi.nr_ssb++;
    }
    if ((s->slot % 20) == 2) {
        s->kpi.nr_sib1++;
        (void)wl_take_cce(s, 4);
    }
}

/* ---------------------------------------------------------------- NR DL */
static void wl_schedule_dl(wl_state *s, double penalty)
{
    const wl_config *cfg = &s->cfg;
    wl_ue *active[WL_MAX_UE];
    wl_ue *order[WL_MAX_UE];
    int n_active = 0, on = 0;
    int alloc_n[WL_MAX_UE];
    uint8_t alloc_rbg[WL_MAX_UE][WL_MAX_RBG];
    int n_rb = cfg->nr_prb;
    int n_rbg = (n_rb + cfg->rbg_size - 1) / cfg->rbg_size;
    int cap, i, g, k, a, b;

    for (i = 0; i < WL_MAX_UE; i++) alloc_n[i] = 0;
    if (n_rbg > WL_MAX_RBG) n_rbg = WL_MAX_RBG;

    for (i = 0; i < s->n_ue; i++) {
        wl_ue *u = &s->ues[i];
        if (u->connected && (u->dl_queue > 0.0 || wl_ue_dl_due(u, s->slot)))
            active[n_active++] = u;
    }
    if (n_active == 0) return;

    cap = n_rbg / (n_active < 6 ? n_active : 6);
    if (cap < 2) cap = 2;

    for (g = 0; g < n_rbg; g++) {
        int lo = g * cfg->rbg_size;
        int hi = lo + cfg->rbg_size < n_rb ? lo + cfg->rbg_size : n_rb;
        int rbs[WL_MAX_RB];
        int n = 0;
        double best = -1.0;
        wl_ue *bu = 0;

        for (k = lo; k < hi; k++) rbs[n++] = k;
        for (i = 0; i < n_active; i++) {
            wl_ue *u = active[i];
            double eff[4], inst, m;
            const wl_mcs *mcs;
            if (alloc_n[u->uid] >= cap) continue;
            wl_eff_sinr(s, u, rbs, n, penalty, 0.0, u->cqi_gain, eff);
            mcs = wl_pick_mcs_eff(g_nr_table, WL_NR_MCS, eff);
            inst = (double)n * (double)cfg->nr_re_per_prb *
                   (double)mcs->qm * (double)mcs->rate / 1024.0;
            m = inst / (u->avg_rate > 1.0 ? u->avg_rate : 1.0);
            if (m > best) {
                best = m;
                bu = u;
            }
        }
        if (!bu) break;
        alloc_rbg[bu->uid][alloc_n[bu->uid]++] = (uint8_t)g;
    }

    for (i = 0; i < n_active; i++)
        if (alloc_n[active[i]->uid] > 0) order[on++] = active[i];

    for (a = 1; a < on; a++) {
        wl_ue *key = order[a];
        int ka = wl_ue_dl_due(key, s->slot) ? 0 : 1;
        int kn = -alloc_n[key->uid];
        b = a - 1;
        while (b >= 0) {
            int kb = wl_ue_dl_due(order[b], s->slot) ? 0 : 1;
            int knb = -alloc_n[order[b]->uid];
            if (kb < ka || (kb == ka && knb <= kn)) break;
            order[b + 1] = order[b];
            b--;
        }
        order[b + 1] = key;
    }

    for (a = 0; a < on; a++) {
        wl_ue *u = order[a];
        wl_harq *proc = 0;
        wl_harq *p;
        int is_retx;
        int rbs[WL_MAX_RB];
        int n_rbs = 0;
        int rank, tbs, flat_tbs;
        double eff[4], eff_true[4], est[4];
        double flat_eff[4], flat_est[4];
        const wl_mcs *mcs;
        const wl_mcs *flat_mcs;
        double tbs_f, flat_f, q, bler_layer, bler;
        int flat_rank, ok;

        for (k = 0; k < WL_DL_HARQ; k++) {
            if (u->harq[k].pending_at >= 0 && u->harq[k].pending_at <= s->slot) {
                proc = &u->harq[k];
                break;
            }
        }
        is_retx = proc != 0;
        if (!is_retx && u->dl_queue <= 0.0) continue;

        for (k = 0; k < alloc_n[u->uid]; k++) {
            int gg = alloc_rbg[u->uid][k];
            int lo = gg * cfg->rbg_size;
            int hi = lo + cfg->rbg_size < n_rb ? lo + cfg->rbg_size : n_rb;
            int x;
            for (x = lo; x < hi; x++) rbs[n_rbs++] = x;
        }
        if (n_rbs <= 0) continue;

        wl_eff_sinr(s, u, rbs, n_rbs, penalty,
                    is_retx ? proc->combining_db : 0.0, u->cqi_gain, eff);
        wl_eff_sinr(s, u, rbs, n_rbs, penalty,
                    is_retx ? proc->combining_db : 0.0, 0, eff_true);

        if (!wl_take_cce(s, wl_cce_level(eff[0]))) continue;
        s->rbg_alloc_sum += alloc_n[u->uid];

        wl_amc_eff(s, u, eff, est);
        rank = is_retx ? proc->rank : wl_pick_rank(s, est);
        wl_layer_sinr(s, est, rank, est);
        mcs = wl_pick_mcs_eff(g_nr_table, WL_NR_MCS, est);
        tbs_f = (double)rank * (double)n_rbs * (double)cfg->nr_re_per_prb *
                (double)mcs->qm * (double)mcs->rate / 1024.0;
        tbs = ((int)tbs_f) & ~7;

        {
            int all[WL_MAX_RB];
            int aa;
            for (aa = 0; aa < n_rb; aa++) all[aa] = aa;
            wl_eff_sinr(s, u, all, n_rb, penalty,
                        is_retx ? proc->combining_db : 0.0, u->cqi_gain, flat_eff);
            wl_amc_eff(s, u, flat_eff, flat_est);
            flat_rank = is_retx ? rank : wl_pick_rank(s, flat_est);
            wl_layer_sinr(s, flat_est, flat_rank, flat_est);
            flat_mcs = wl_pick_mcs_eff(g_nr_table, WL_NR_MCS, flat_est);
            flat_f = (double)flat_rank * (double)n_rbs * (double)cfg->nr_re_per_prb *
                     (double)flat_mcs->qm * (double)flat_mcs->rate / 1024.0;
            flat_tbs = ((int)flat_f) & ~7;
        }

        q = u->dl_queue > 0.0 ? u->dl_queue : 0.0;
        if (is_retx) {
            tbs = proc->tbs;
            flat_tbs = proc->tbs;
        } else {
            double tbs_full = (double)tbs;
            double flat_full = (double)flat_tbs;
            tbs = ((int)((double)tbs < q ? (double)tbs : q)) & ~7;
            flat_tbs = ((int)((double)flat_tbs < q ? (double)flat_tbs : q)) & ~7;
            if (tbs <= 0) {
                int m = (int)q;
                if (m > 8) m = 8;
                tbs = m;
                flat_tbs = m;
            }
            if (u->dl_head_slot < 0) u->dl_head_slot = s->slot;
            wl_lat_add(&u->dl_lat_n, &u->dl_lat_sum, &u->dl_lat_max,
                       (double)(s->slot - u->dl_head_slot));
            s->fs_bits += tbs_full;
            s->flat_bits += flat_full;
        }

        s->kpi.nr_pdcch++;
        s->kpi.nr_dl_tb++;
        if (is_retx) {
            s->kpi.nr_dl_retx++;
            u->dl_retx++;
        } else {
            s->kpi.nr_dl_tb_first++;
            s->kpi.nr_layers_sum += rank;
            if (rank == 2) s->kpi.nr_rank2++;
        }
        s->nr_tb_cnt++;

        {
            double lyr[4];
            wl_layer_sinr(s, eff_true, rank, lyr);
            bler_layer = wl_bler_req(lyr[wl_qm_index(mcs->qm)] - cfg->mismatch_db,
                                     mcs->req);
            bler = 1.0 - wl_pow_int(1.0 - bler_layer, rank);
            s->nr_bler_sum += bler;
            if (cfg->phy.enable) {
                /* 真实链路：同一 SNR 下跑一遍 OFDM + FEC，ACK/NACK 由 CRC 决定 */
                uint64_t pseed = (uint64_t)(uint32_t)cfg->seed * 1000003ULL +
                                 (uint64_t)(uint32_t)s->slot * 131ULL +
                                 (uint64_t)(uint32_t)u->uid * 17ULL +
                                 (is_retx ? 7ULL : 0ULL);
                ok = wl_phy_link(&s->phy_ctx, &cfg->phy,
                                 lyr[wl_qm_index(mcs->qm)],
                                 mcs->qm, (double)mcs->rate / 1024.0, pseed);
            } else {
                ok = wl_rng_random(&s->rng) > bler;
            }
            if (!is_retx) wl_olla_update(s, u, ok);
        }

        if (ok) {
            u->dl_bits += (uint64_t)tbs;
            u->dl_tbs++;
            s->kpi.nr_dl_bits += (uint64_t)tbs;
            u->avg_rate = 0.9 * u->avg_rate + 0.1 * (double)tbs;
            if (!is_retx) {
                u->dl_queue -= (double)tbs;
                if (u->dl_queue < 0.0) u->dl_queue = 0.0;
                if (u->dl_queue <= 0.0) u->dl_head_slot = -1;
            }
            if (proc) {
                proc->pending_at = -1;
                proc->rv = 0;
                proc->combining_db = 0.0;
                proc->attempts = 0;
            }
        } else {
            s->kpi.nr_dl_nack++;
            u->dl_nack++;
            if (!is_retx) s->kpi.nr_dl_nack_first++;
            p = proc;
            if (!p) {
                p = &u->harq[0];
                for (k = 0; k < WL_DL_HARQ; k++) {
                    if (u->harq[k].pending_at < 0) {
                        p = &u->harq[k];
                        break;
                    }
                }
            }
            p->tbs = tbs;
            if (!is_retx) p->rank = rank;
            p->attempts++;
            p->rv = (p->rv + 2) % 4;
            p->combining_db = 3.0 * (double)p->attempts < 9.0 ?
                              3.0 * (double)p->attempts : 9.0;
            p->pending_at = s->slot + 3;
        }
    }
}

/* ---------------------------------------------------------------- NR UL */
static void wl_schedule_ul(wl_state *s, double penalty)
{
    const wl_config *cfg = &s->cfg;
    wl_ue *due[WL_MAX_UE];
    wl_ue *nw[WL_MAX_UE];
    wl_ue *order[WL_MAX_UE];
    int nd = 0, nn = 0, no = 0;
    int i, j, k;
    int start, n, prb;

    for (i = 0; i < s->n_ue; i++) {
        wl_ue *u = &s->ues[i];
        int has_due = 0, has_free = 0;
        if (!u->connected) continue;
        for (k = 0; k < WL_UL_HARQ; k++) {
            if (u->ul_harq[k].pending_at >= 0 && u->ul_harq[k].pending_at <= s->slot)
                has_due = 1;
            if (u->ul_harq[k].pending_at < 0) has_free = 1;
        }
        if (has_due) due[nd++] = u;
        else if (u->ul_queue > 0.0 && has_free) nw[nn++] = u;
    }
    for (i = 0; i < nd; i++) order[no++] = due[i];
    for (i = 0; i < nn; i++) order[no++] = nw[i];
    if (no == 0) return;

    start = s->ul_rr % s->n_ue;
    for (i = 1; i < no; i++) {
        wl_ue *key = order[i];
        int kk = (key->uid - start + s->n_ue) % s->n_ue;
        j = i - 1;
        while (j >= 0 && ((order[j]->uid - start + s->n_ue) % s->n_ue) > kk) {
            order[j + 1] = order[j];
            j--;
        }
        order[j + 1] = key;
    }
    s->ul_rr++;

    n = no < cfg->nr_prb / 16 ? no : cfg->nr_prb / 16;
    if (n < 1) n = 1;
    prb = cfg->nr_prb / (n > 0 ? n : 1);
    if (prb < 1) prb = 1;

    for (i = 0; i < n && i < no; i++) {
        wl_ue *u = order[i];
        int rbs[WL_MAX_RB];
        wl_harq *proc = 0;
        int is_retx;
        int tbs, a;
        double extra, eff[4], meas, bler;
        int ok;

        for (a = 0; a < prb; a++) rbs[a] = (i * prb + a) % cfg->nr_prb;
        for (k = 0; k < WL_UL_HARQ; k++) {
            if (u->ul_harq[k].pending_at >= 0 && u->ul_harq[k].pending_at <= s->slot) {
                proc = &u->ul_harq[k];
                break;
            }
        }
        is_retx = proc != 0;
        if (!is_retx) {
            for (k = 0; k < WL_UL_HARQ; k++) {
                if (u->ul_harq[k].pending_at < 0) {
                    proc = &u->ul_harq[k];
                    break;
                }
            }
        }
        if (!proc) continue;

        extra = u->ul_power_db - cfg->ul_loss_db +
                (is_retx ? proc->combining_db : 0.0);
        wl_eff_sinr(s, u, rbs, prb, penalty, extra, 0, eff);
        {
            double vals[WL_MAX_RB];
            for (a = 0; a < prb; a++)
                vals[a] = wl_ch_sinr_rb(&u->ch, rbs[a], penalty) + extra;
            meas = wl_lin_db(vals, prb);
        }
        if (is_retx) {
            tbs = proc->tbs;
        } else {
            const wl_mcs *mcs = wl_pick_mcs_eff(g_nr_table, WL_NR_MCS, eff);
            double tf = (double)prb * (double)cfg->nr_re_per_prb *
                        (double)mcs->qm * (double)mcs->rate / 1024.0;
            double q = u->ul_queue < tf ? u->ul_queue : tf;
            tbs = ((int)q) & ~7;
            if (tbs <= 0) continue;
            proc->req_db = mcs->req;
            proc->qm = mcs->qm;
        }
        if (!wl_take_cce(s, wl_cce_level(eff[0]))) continue;
        s->kpi.nr_pdcch++;

        if (cfg->ul_pc) {
            double err = cfg->ul_target_sinr_db - meas;
            double step = err > 0.0 ? cfg->ul_pc_step_db : -cfg->ul_pc_step_db;
            u->ul_power_db += step;
            if (u->ul_power_db > cfg->ul_pc_max_db) u->ul_power_db = cfg->ul_pc_max_db;
            if (u->ul_power_db < -cfg->ul_pc_max_db) u->ul_power_db = -cfg->ul_pc_max_db;
        }

        s->kpi.nr_ul_tb++;
        if (is_retx) {
            s->kpi.nr_ul_retx++;
            u->ul_retx++;
        } else {
            proc->tbs = tbs;
            proc->attempts = 0;
            proc->rv = 0;
            proc->combining_db = 0.0;
            proc->first_slot = s->slot;
            s->kpi.nr_ul_tb_first++;
            u->ul_sinr_sum += meas;
            u->ul_sinr_cnt++;
        }
        bler = wl_bler_req(eff[wl_qm_index(proc->qm)], proc->req_db);
        s->ul_bler_sum += bler;
        ok = wl_rng_random(&s->rng) > bler;
        if (ok) {
            u->ul_queue -= (double)proc->tbs;
            if (u->ul_queue < 0.0) u->ul_queue = 0.0;
            u->ul_bits += (uint64_t)proc->tbs;
            s->kpi.nr_ul_bits += (uint64_t)proc->tbs;
            if (proc->first_slot >= 0)
                wl_lat_add(&u->ul_lat_n, &u->ul_lat_sum, &u->ul_lat_max,
                           (double)(s->slot - proc->first_slot));
            proc->pending_at = -1;
            proc->rv = 0;
            proc->combining_db = 0.0;
            proc->attempts = 0;
            proc->first_slot = -1;
        } else {
            s->kpi.nr_ul_nack++;
            u->ul_nack++;
            if (!is_retx) s->kpi.nr_ul_nack_first++;
            proc->attempts++;
            proc->rv = (proc->rv + 2) % 4;
            proc->combining_db = 3.0 * (double)proc->attempts < 9.0 ?
                                 3.0 * (double)proc->attempts : 9.0;
            if (proc->attempts >= 4) {
                s->kpi.nr_ul_drop++;
                u->ul_queue -= (double)proc->tbs;
                if (u->ul_queue < 0.0) u->ul_queue = 0.0;
                proc->pending_at = -1;
                proc->attempts = 0;
                proc->combining_db = 0.0;
                proc->rv = 0;
                proc->first_slot = -1;
            } else {
                proc->pending_at = s->slot + cfg->ul_harq_rt_slots;
            }
        }
    }
}

/* ---------------------------------------------------------------- PRACH */
static void wl_prach(wl_state *s)
{
    wl_ue *cand[WL_MAX_UE];
    int nc = 0, i, k;

    if ((s->slot % 10) != 0) return;

    for (i = 0; i < s->n_ue; i++) {
        wl_ue *u = &s->ues[i];
        if (u->ra_stage == 1 && s->slot - u->ra_try_slot >= 4) {
            u->ra_stage = 0;
            u->connected = 1;
        }
    }
    for (i = 0; i < s->n_ue; i++)
        if (!s->ues[i].connected && s->ues[i].ra_stage == 0) cand[nc++] = &s->ues[i];

    if (nc == 0 && s->slot > 0 && (s->slot % 100) == 0 && s->n_ue > 0) {
        wl_ue *u = &s->ues[wl_rng_randrange(&s->rng, s->n_ue)];
        u->connected = 0;
        u->ra_stage = 0;
        cand[nc++] = u;
    }
    if (nc == 0) return;

    s->kpi.nr_prach++;

    {
        int pre[WL_MAX_UE], used[WL_MAX_UE], cnt[WL_MAX_UE];
        int nu = 0;
        for (i = 0; i < nc; i++) {
            int p = wl_rng_randrange(&s->rng, 64);
            int f = 0;
            for (k = 0; k < nu; k++) {
                if (used[k] == p) {
                    cnt[k]++;
                    f = 1;
                    break;
                }
            }
            if (!f) {
                used[nu] = p;
                cnt[nu] = 1;
                nu++;
            }
            pre[i] = p;
        }
        for (i = 0; i < nc; i++) {
            int idx = -1;
            for (k = 0; k < nu; k++) {
                if (used[k] == pre[i]) {
                    idx = k;
                    break;
                }
            }
            if (idx < 0 || cnt[idx] > 1) {
                s->kpi.nr_ra_fail++;
                continue;
            }
            if (wl_rng_random(&s->rng) < wl_bler_of(cand[i]->snr_db, 2, 120)) {
                s->kpi.nr_ra_fail++;
                continue;
            }
            cand[i]->ra_stage = 1;
            cand[i]->ra_try_slot = s->slot;
            s->kpi.nr_ra_ok++;
        }
    }
}

/* ---------------------------------------------------------------- LTE */
static void wl_lte_tick(wl_state *s)
{
    const wl_config *cfg = &s->cfg;
    wl_ue *active[WL_MAX_UE];
    int n = 0, i, step, prb, subset_n;
    if (!cfg->scenario_mixed) return;

    for (i = 0; i < s->n_ue; i++)
        if (s->ues[i].connected) active[n++] = &s->ues[i];
    if (n == 0) return;

    subset_n = (n + 1) / 2;
    if (subset_n > cfg->lte_prb / 10) subset_n = cfg->lte_prb / 10;
    if (subset_n < 1) subset_n = 1;
    prb = cfg->lte_prb / subset_n;
    if (prb < 1) prb = 1;

    for (i = 0, step = 0; i < subset_n; i++, step += 2) {
        wl_ue *u = active[step < n ? step : n - 1];
        int all[WL_MAX_RB], a, tbs;
        double eff[4], tf, bler;
        const wl_mcs *mcs;

        for (a = 0; a < cfg->lte_prb; a++) all[a] = a;
        wl_eff_sinr(s, u, all, cfg->lte_prb, 0.0, 0.0, 0, eff);
        mcs = wl_pick_mcs_eff(g_lte_table, WL_LTE_MCS, eff);
        tf = (double)prb * (double)cfg->lte_re_per_prb *
             (double)mcs->qm * (double)mcs->rate / 1024.0;
        {
            double q = u->dl_queue > 0.0 ? u->dl_queue : 0.0;
            tbs = ((int)(tf < q ? tf : q)) & ~7;
        }
        if (tbs <= 0) continue;

        s->kpi.lte_dl_tb++;
        bler = wl_bler_req(eff[wl_qm_index(mcs->qm)] - cfg->mismatch_db, mcs->req);
        if (wl_rng_random(&s->rng) > bler) {
            u->dl_queue -= (double)tbs;
            if (u->dl_queue < 0.0) u->dl_queue = 0.0;
            u->dl_bits += (uint64_t)tbs;
            s->kpi.lte_dl_bits += (uint64_t)tbs;
        } else {
            s->kpi.lte_nack++;
        }
    }
}

/* ---------------------------------------------------------------- WiFi */
static double wl_wifi_tick(wl_state *s, int nr_gets)
{
    double penalty = 0.0;
    int i;
    if (!s->cfg.scenario_mixed) return 0.0;

    for (i = 0; i < s->n_wifi; i++) {
        wl_wifi *sta = &s->wifi[i];
        const wl_mcs *mcs;
        int bits;

        if (sta->backoff > 0) {
            sta->backoff--;
            continue;
        }
        if (wl_rng_random(&s->rng) > s->cfg.wifi_attempt_p) {
            sta->backoff = wl_rng_randint(&s->rng, 1, 8);
            continue;
        }
        sta->attempts++;
        s->kpi.wifi_attempt++;

        if (s->cfg.wifi_shared_band && nr_gets) {
            if (wl_rng_random(&s->rng) < 0.2) {
                sta->collisions++;
                s->kpi.wifi_coll++;
                if (penalty < 6.0) penalty = 6.0;
                sta->backoff = wl_rng_randint(&s->rng, 1, 8);
            } else {
                sta->backoff = 1;
            }
            continue;
        }

        mcs = wl_pick_mcs(g_nr_table, WL_NR_MCS, sta->snr_db);
        bits = (int)(1500.0 * 8.0 *
                     (0.35 + 0.65 * (double)mcs->qm * (double)mcs->rate / 948.0));
        sta->tx_bits += (uint64_t)bits;
        s->kpi.wifi_bits += (uint64_t)bits;
    }
    return penalty;
}

/* ---------------------------------------------------------------- 空口需求 */
static int wl_nr_wants_air(const wl_state *s)
{
    int i;
    if ((s->slot % 20) == 0 || (s->slot % 20) == 2) return 1;
    for (i = 0; i < s->n_ue; i++) {
        const wl_ue *u = &s->ues[i];
        if (u->connected && (u->dl_queue > 0.0 || u->ul_queue > 0.0)) return 1;
    }
    return 0;
}

/* ---------------------------------------------------------------- 时隙推进 */
static void wl_slot_channels(wl_state *s)
{
    int i;
    for (i = 0; i < s->n_ue; i++) {
        wl_ue *u = &s->ues[i];
        int rb, old, cap;

        if (u->ch.fd > 0.0) {
            wl_ch_advance(&u->ch);
        } else if ((s->slot % s->cfg.coherence_slots) == 0) {
            wl_ch_realize(&u->ch, &s->rng);
        }

        cap = s->cfg.cqi_delay_slots;
        u->cqi_head = (u->cqi_head + 1) % cap;
        for (rb = 0; rb < u->ch.n_rb; rb++)
            u->cqi_hist[u->cqi_head][rb] = u->ch.gain_db[rb];
        if (u->cqi_n < cap) u->cqi_n++;
        old = (u->cqi_head - u->cqi_n + 1 + cap * 2) % cap;
        for (rb = 0; rb < u->ch.n_rb; rb++)
            u->cqi_gain[rb] = u->cqi_hist[old][rb];
    }
}

static void wl_slot_arrivals(wl_state *s)
{
    int i;
    for (i = 0; i < s->n_ue; i++) {
        wl_ue *u = &s->ues[i];
        if (s->cfg.dl_full_buffer) u->dl_queue += s->cfg.dl_bits_per_slot;
        if (wl_rng_random(&s->rng) < s->cfg.ul_arrival_p) u->ul_queue += s->cfg.ul_bits;
        if (u->dl_queue > 200000.0) u->dl_queue = 200000.0;
        if (u->ul_queue > 20000.0) u->ul_queue = 20000.0;
    }
}

void wl_slot(wl_state *s)
{
    int wants, gets;
    double penalty;

    s->cce_used = 0;
    wl_slot_channels(s);
    wl_slot_arrivals(s);

    wants = wl_nr_wants_air(s);
    gets = wants && (!s->cfg.wifi_shared_band || wl_rng_random(&s->rng) < 0.7);
    penalty = wl_wifi_tick(s, gets);

    if (gets) {
        wl_broadcast(s);
        wl_schedule_dl(s, penalty);
        wl_schedule_ul(s, penalty);
        wl_prach(s);
    }
    wl_lte_tick(s);

    s->cce_sum += s->cce_used;
    s->slot++;
}

void wl_run(wl_state *s)
{
    while (s->slot < s->cfg.slots) wl_slot(s);
}

/* ---------------------------------------------------------------- 结果打包 */
static uint32_t wl_ppm(double num, double den)
{
    double v;
    if (den <= 0.0) return 0;
    v = num * 1000000.0 / den;
    if (v <= 0.0) return 0;
    if (v > 4294967295.0) return 4294967295u;
    return (uint32_t)(v + 0.5);
}

static uint32_t wl_i32(double v)
{
    return (uint32_t)(int32_t)wl_round_i(v);
}

static void wl_put_u64(uint32_t *w, int idx, uint64_t v)
{
    w[idx] = (uint32_t)(v & 0xFFFFFFFFu);
    w[idx + 1] = (uint32_t)(v >> 32);
}

static uint32_t wl_clamp_u32(double v)
{
    if (v <= 0.0) return 0;
    if (v > 4294967295.0) return 4294967295u;
    return (uint32_t)(v + 0.5);
}

static uint32_t wl_hash_u32(uint32_t h, uint32_t v)
{
    h ^= v;
    h *= 16777619u;
    return h;
}

static uint32_t wl_hash_milli(uint32_t h, double v)
{
    return wl_hash_u32(h, (uint32_t)(int32_t)wl_round_i(v * 1000.0));
}

/* 配置指纹：防止旧二进制跑新参数。只用整数/毫单位，跨平台一致。 */
static uint32_t wl_config_hash(const wl_config *c)
{
    uint32_t h = 2166136261u;
    h = wl_hash_u32(h, (uint32_t)c->slots);
    h = wl_hash_u32(h, (uint32_t)c->n_ue);
    h = wl_hash_u32(h, (uint32_t)c->seed);
    h = wl_hash_u32(h, (uint32_t)c->nr_prb);
    h = wl_hash_u32(h, (uint32_t)c->nr_re_per_prb);
    h = wl_hash_u32(h, (uint32_t)c->coreset_rb);
    h = wl_hash_u32(h, (uint32_t)c->coreset_sym);
    h = wl_hash_u32(h, (uint32_t)c->rbg_size);
    h = wl_hash_u32(h, (uint32_t)c->max_layers);
    h = wl_hash_u32(h, (uint32_t)c->sinr_map_lin);
    h = wl_hash_u32(h, (uint32_t)c->olla);
    h = wl_hash_milli(h, c->mismatch_db);
    h = wl_hash_u32(h, (uint32_t)c->lte_prb);
    h = wl_hash_u32(h, (uint32_t)c->lte_re_per_prb);
    h = wl_hash_u32(h, (uint32_t)c->wifi_sta);
    h = wl_hash_u32(h, (uint32_t)c->wifi_shared_band);
    h = wl_hash_u32(h, (uint32_t)c->dl_full_buffer);
    h = wl_hash_u32(h, (uint32_t)c->ul_harq_rt_slots);
    h = wl_hash_u32(h, (uint32_t)c->ul_pc);
    h = wl_hash_u32(h, (uint32_t)c->cqi_delay_slots);
    h = wl_hash_u32(h, (uint32_t)c->coherence_slots);
    h = wl_hash_u32(h, (uint32_t)c->scenario_mixed);
    h = wl_hash_milli(h, c->mimo_layer_loss_db);
    h = wl_hash_milli(h, c->olla_step_up);
    h = wl_hash_milli(h, c->olla_step_down);
    h = wl_hash_milli(h, c->olla_max_db);
    h = wl_hash_milli(h, c->wifi_attempt_p);
    h = wl_hash_milli(h, c->dl_bits_per_slot);
    h = wl_hash_milli(h, c->ul_arrival_p);
    h = wl_hash_milli(h, c->ul_bits);
    h = wl_hash_milli(h, c->ul_target_sinr_db);
    h = wl_hash_milli(h, c->ul_pc_step_db);
    h = wl_hash_milli(h, c->ul_pc_max_db);
    h = wl_hash_milli(h, c->ul_loss_db);
    h = wl_hash_milli(h, c->delay_spread_ns);
    h = wl_hash_milli(h, c->speed_kmh);
    h = wl_hash_milli(h, c->carrier_ghz);
    h = wl_hash_milli(h, c->slot_ms);
    h = wl_hash_u32(h, (uint32_t)c->phy.enable);
    h = wl_hash_u32(h, (uint32_t)c->phy.max_prb);
    h = wl_hash_u32(h, (uint32_t)c->phy.target_coded);
    h = wl_hash_u32(h, (uint32_t)c->phy.n_sym);
    h = wl_hash_u32(h, (uint32_t)c->phy.ce_mode);
    h = wl_hash_milli(h, c->phy.noise_fig_db);
    return h;
}

void wl_result(const wl_state *s, uint32_t *words)
{
    const wl_kpi *k = &s->kpi;
    double lat_sum = 0.0, lat_max = 0.0, ul_lat_sum = 0.0;
    int64_t lat_n = 0, ul_lat_n = 0;
    double served_sum = 0.0, served_sq = 0.0;
    double spread = 0.0, rbg_spread = 0.0;
    double olla_mean = 0.0, ul_power_mean = 0.0, ul_sinr_mean = 0.0;
    int n_sinr = 0;
    int i;

    wl_memzero(words, (uint64_t)WL_RESULT_WORDS * 4u);

    words[WL_R_MAGIC] = WL_RESULT_MAGIC;
    words[WL_R_SCHEMA] = WL_SCHEMA;
    words[WL_R_SLOTS] = (uint32_t)s->cfg.slots;
    words[WL_R_UES] = (uint32_t)s->n_ue;

    words[WL_R_NR_DL_TB] = (uint32_t)k->nr_dl_tb;
    words[WL_R_NR_DL_TB_FIRST] = (uint32_t)k->nr_dl_tb_first;
    words[WL_R_NR_DL_RETX] = (uint32_t)k->nr_dl_retx;
    words[WL_R_NR_DL_NACK] = (uint32_t)k->nr_dl_nack;
    words[WL_R_NR_DL_NACK_FIRST] = (uint32_t)k->nr_dl_nack_first;
    words[WL_R_NR_RANK2] = (uint32_t)k->nr_rank2;
    words[WL_R_NR_LAYERS_SUM] = (uint32_t)k->nr_layers_sum;
    wl_put_u64(words, WL_R_NR_DL_BITS_LO, k->nr_dl_bits);

    words[WL_R_NR_UL_TB] = (uint32_t)k->nr_ul_tb;
    words[WL_R_NR_UL_TB_FIRST] = (uint32_t)k->nr_ul_tb_first;
    words[WL_R_NR_UL_NACK] = (uint32_t)k->nr_ul_nack;
    words[WL_R_NR_UL_NACK_FIRST] = (uint32_t)k->nr_ul_nack_first;
    words[WL_R_NR_UL_RETX] = (uint32_t)k->nr_ul_retx;
    words[WL_R_NR_UL_DROP] = (uint32_t)k->nr_ul_drop;
    wl_put_u64(words, WL_R_NR_UL_BITS_LO, k->nr_ul_bits);

    words[WL_R_NR_PDCCH] = (uint32_t)k->nr_pdcch;
    words[WL_R_NR_SSB] = (uint32_t)k->nr_ssb;
    words[WL_R_NR_SIB1] = (uint32_t)k->nr_sib1;
    words[WL_R_NR_PRACH] = (uint32_t)k->nr_prach;
    words[WL_R_NR_RA_OK] = (uint32_t)k->nr_ra_ok;
    words[WL_R_NR_RA_FAIL] = (uint32_t)k->nr_ra_fail;

    words[WL_R_LTE_TB] = (uint32_t)k->lte_dl_tb;
    words[WL_R_LTE_NACK] = (uint32_t)k->lte_nack;
    wl_put_u64(words, WL_R_LTE_BITS_LO, k->lte_dl_bits);

    words[WL_R_WIFI_ATT] = (uint32_t)k->wifi_attempt;
    words[WL_R_WIFI_COLL] = (uint32_t)k->wifi_coll;
    wl_put_u64(words, WL_R_WIFI_BITS_LO, k->wifi_bits);

    words[WL_R_NR_NACK_PPM] = wl_ppm((double)k->nr_dl_nack,
                                     (double)(k->nr_dl_tb > 0 ? k->nr_dl_tb : 1));
    words[WL_R_NR_FIRST_BLER_PPM] = wl_ppm((double)k->nr_dl_nack_first,
                                           (double)(k->nr_dl_tb_first > 0 ? k->nr_dl_tb_first : 1));
    words[WL_R_NR_EXP_BLER_PPM] = wl_ppm(s->nr_bler_sum,
                                         (double)(s->nr_tb_cnt > 0 ? s->nr_tb_cnt : 1));
    words[WL_R_UL_NACK_PPM] = wl_ppm((double)k->nr_ul_nack,
                                     (double)(k->nr_ul_tb > 0 ? k->nr_ul_tb : 1));
    words[WL_R_UL_FIRST_BLER_PPM] = wl_ppm((double)k->nr_ul_nack_first,
                                           (double)(k->nr_ul_tb_first > 0 ? k->nr_ul_tb_first : 1));
    words[WL_R_UL_EXP_BLER_PPM] = wl_ppm(s->ul_bler_sum,
                                         (double)(k->nr_ul_tb > 0 ? k->nr_ul_tb : 1));

    for (i = 0; i < s->n_ue; i++) {
        const wl_ue *u = &s->ues[i];
        lat_n += u->dl_lat_n;
        lat_sum += u->dl_lat_sum;
        if (u->dl_lat_max > lat_max) lat_max = u->dl_lat_max;
        ul_lat_n += u->ul_lat_n;
        ul_lat_sum += u->ul_lat_sum;
        served_sum += (double)u->dl_bits;
        served_sq += (double)u->dl_bits * (double)u->dl_bits;
        spread += wl_ch_spread_db(&u->ch);
        rbg_spread += wl_ch_rbg_spread_db(&u->ch, s->cfg.rbg_size);
        olla_mean += u->olla_db;
        ul_power_mean += u->ul_power_db;
        if (u->ul_sinr_cnt > 0) {
            ul_sinr_mean += u->ul_sinr_sum / (double)u->ul_sinr_cnt;
            n_sinr++;
        }
    }
    if (s->n_ue > 0) {
        spread /= (double)s->n_ue;
        rbg_spread /= (double)s->n_ue;
        olla_mean /= (double)s->n_ue;
        ul_power_mean /= (double)s->n_ue;
    }
    if (n_sinr > 0) ul_sinr_mean /= (double)n_sinr;

    words[WL_R_LAT_MEAN_MSLOT] = lat_n > 0 ? wl_clamp_u32(lat_sum / (double)lat_n * 1000.0) : 0;
    words[WL_R_LAT_MAX_MSLOT] = wl_clamp_u32(lat_max * 1000.0);
    words[WL_R_UL_LAT_MEAN_MSLOT] = ul_lat_n > 0 ?
        wl_clamp_u32(ul_lat_sum / (double)ul_lat_n * 1000.0) : 0;

    if (served_sum > 0.0 && s->n_ue > 0)
        words[WL_R_JAIN_PPM] = wl_ppm(served_sum * served_sum,
                                      (double)s->n_ue * served_sq);
    else
        words[WL_R_JAIN_PPM] = 0;

    words[WL_R_FS_GAIN_PPM] = s->flat_bits > 0.0 ?
        wl_i32((s->fs_bits / s->flat_bits - 1.0) * 1000000.0) : 0;
    words[WL_R_CCE_UTIL_PPM] = (s->cfg.slots > 0 && s->cce_cap > 0) ?
        wl_ppm((double)s->cce_sum / (double)s->cfg.slots, (double)s->cce_cap) : 0;
    words[WL_R_CCE_BLOCKED] = (uint32_t)s->cce_blocked;
    words[WL_R_OLLA_MEAN_MDB] = wl_i32(olla_mean * 1000.0);
    words[WL_R_UL_POWER_MEAN_MDB] = wl_i32(ul_power_mean * 1000.0);
    words[WL_R_UL_SINR_MEAN_MDB] = wl_i32(ul_sinr_mean * 1000.0);
    words[WL_R_LAYERS_MEAN_MILLI] = k->nr_dl_tb_first > 0 ?
        wl_clamp_u32((double)k->nr_layers_sum / (double)k->nr_dl_tb_first * 1000.0) : 0;
    words[WL_R_RBG_PER_TB_MILLI] = k->nr_dl_tb > 0 ?
        wl_clamp_u32((double)s->rbg_alloc_sum / (double)k->nr_dl_tb * 1000.0) : 0;
    words[WL_R_SPREAD_MDB] = wl_i32(spread * 1000.0);
    words[WL_R_RBG_SPREAD_MDB] = wl_i32(rbg_spread * 1000.0);
    words[WL_R_CONFIG_HASH] = wl_config_hash(&s->cfg);

    /* ---- PHY 结果块 ---- */
    {
        const wl_phy_stats *ph = &s->phy_ctx.st;
        int64_t links = ph->tb_ok + ph->tb_fail;
        double evm = ph->evm_n > 0 ? ph->evm_acc / (double)ph->evm_n : 0.0;
        double nmse = ph->nmse_n > 0 ? ph->nmse_acc / (double)ph->nmse_n : 0.0;
        double snr = ph->snr_n > 0 ? ph->snr_acc / (double)ph->snr_n : -99.0;
        words[WL_R_PHY_TB_OK] = (uint32_t)ph->tb_ok;
        words[WL_R_PHY_TB_FAIL] = (uint32_t)ph->tb_fail;
        wl_put_u64(words, WL_R_PHY_BITS_LO, ph->bits_ok);
        words[WL_R_PHY_BLER_PPM] = wl_ppm((double)ph->tb_fail,
                                         (double)(links > 0 ? links : 1));
        words[WL_R_PHY_RAW_BER_PPM] = wl_ppm((double)ph->raw_err,
                                            (double)(ph->raw_bits > 0 ? ph->raw_bits : 1));
        words[WL_R_PHY_POST_BER_PPM] = wl_ppm((double)ph->dec_err,
                                             (double)(ph->dec_bits > 0 ? ph->dec_bits : 1));
        words[WL_R_PHY_EVM_MDB] = wl_i32(evm > 1e-9 ? 20.0 * wl_log10(evm) * 1000.0 : 0.0);
        words[WL_R_PHY_NMSE_MDB] = wl_i32(nmse > 1e-9 ? 10.0 * wl_log10(nmse) * 1000.0 : 0.0);
        words[WL_R_PHY_SNR_EST_MDB] = wl_i32(snr * 1000.0);
        words[WL_R_PHY_RATE_MILLI] = wl_clamp_u32(ph->rate_n > 0 ?
            ph->rate_acc / (double)ph->rate_n * 1000.0 : 0.0);
        words[WL_R_PHY_FFT_OPS] = (uint32_t)ph->fft_ops;
        words[WL_R_PHY_VITERBI_OPS] = (uint32_t)ph->viterbi_ops;
        words[WL_R_PHY_LINKS] = (uint32_t)links;
        words[WL_R_PHY_PRB_MILLI] = wl_clamp_u32(links > 0 ?
            (double)ph->prb_sum / (double)links * 1000.0 : 0.0);
        words[WL_R_PHY_RESERVED] = 0;
    }

    for (i = 0; i < s->n_ue; i++) {
        const wl_ue *u = &s->ues[i];
        int base = WL_R_UE_BASE + i * WL_UE_STRIDE;
        wl_put_u64(words, base + WL_UE_DL_BITS_LO, u->dl_bits);
        wl_put_u64(words, base + WL_UE_UL_BITS_LO, u->ul_bits);
        words[base + WL_UE_DL_QUEUE] = wl_clamp_u32(u->dl_queue);
        words[base + WL_UE_UL_QUEUE] = wl_clamp_u32(u->ul_queue);
        words[base + WL_UE_DL_NACK] = (uint32_t)u->dl_nack;
        words[base + WL_UE_UL_NACK] = (uint32_t)u->ul_nack;
        words[base + WL_UE_OLLA_MDB] = wl_i32(u->olla_db * 1000.0);
        words[base + WL_UE_UL_POWER_MDB] = wl_i32(u->ul_power_db * 1000.0);
    }
}

int wl_result_words(void)
{
    return WL_RESULT_WORDS;
}
