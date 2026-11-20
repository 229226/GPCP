/* wlbench 主机运行器：只做参数解析、调用核心、打印/落盘结果。
 *
 * 硬件后端不需要复用这个文件；它只需要同样调用 wl_init/wl_run/wl_result
 * 并把结果字按 index value 格式输出（或写到固定内存区）。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "wlbench.h"

static wl_state g_state;

static int on_off(const char *s)
{
    return s && (strcmp(s, "on") == 0 || strcmp(s, "1") == 0 ||
                 strcmp(s, "true") == 0);
}

static uint64_t rd64(const uint32_t *w, int idx)
{
    return (uint64_t)w[idx] | ((uint64_t)w[idx + 1] << 32);
}

static void usage(void)
{
    printf(
        "wlbench —— 硬件无关的无线协议整机 benchmark\n"
        "用法: wlbench_host [选项]\n"
        "  --scenario mixed|nr-only\n"
        "  --slots N --ues N --seed N\n"
        "  --dl-bits N --ul-p F --ul-bits N\n"
        "  --speed-kmh F --delay-spread NS --carrier-ghz F --cqi-delay N\n"
        "  --coreset-rb N --coreset-sym N --rbg-size N\n"
        "  --layers N --mimo-loss-db F --sinr-map eesm|lin\n"
        "  --olla on|off --mismatch-db F\n"
        "  --ul-pc on|off --ul-loss-db F --ul-target F\n"
        "  --wifi-sta N --wifi-shared on|off\n"
        "  --lte-prb N --slot-ms F\n"
        "  --phy on|off     物理层链路（默认 on）\n"
        "  --phy-prb N      单次链路的 PRB 上限\n"
        "  --phy-bits N     单次链路的目标编码比特数\n"
        "  --phy-ce 0|1     信道估计 0=LS+插值 1=加频域平滑\n"
        "  --result FILE    结果字（index value，每行一个）\n"
        "  --quiet          只写结果，不打印摘要\n"
        "  --help\n");
}

int main(int argc, char **argv)
{
    wl_config c;
    uint32_t words[WL_RESULT_WORDS];
    const char *result_path = 0;
    int quiet = 0;
    int i;
    double secs;
    double nr_dl_mbps, nr_ul_mbps, lte_mbps, wifi_mbps;
    double nack, first_bler, jain, fs_gain, cce_util;

    wl_default_config(&c);

    for (i = 1; i < argc; i++) {
        const char *k = argv[i];
        const char *v = (i + 1 < argc) ? argv[i + 1] : 0;

        if (!strcmp(k, "--help") || !strcmp(k, "-h")) {
            usage();
            return 0;
        } else if (!strcmp(k, "--quiet")) {
            quiet = 1;
        } else if (!v) {
            fprintf(stderr, "缺少参数值: %s\n", k);
            return 1;
        } else if (!strcmp(k, "--scenario")) {
            if (!strcmp(v, "nr-only")) {
                c.scenario_mixed = 0;
                c.wifi_sta = 0;
            } else {
                c.scenario_mixed = 1;
            }
            i++;
        } else if (!strcmp(k, "--slots")) {
            c.slots = atoi(v); i++;
        } else if (!strcmp(k, "--ues")) {
            c.n_ue = atoi(v); i++;
        } else if (!strcmp(k, "--seed")) {
            c.seed = atoi(v); i++;
        } else if (!strcmp(k, "--dl-bits")) {
            c.dl_bits_per_slot = atof(v); i++;
        } else if (!strcmp(k, "--ul-p")) {
            c.ul_arrival_p = atof(v); i++;
        } else if (!strcmp(k, "--ul-bits")) {
            c.ul_bits = atof(v); i++;
        } else if (!strcmp(k, "--speed-kmh")) {
            c.speed_kmh = atof(v); i++;
        } else if (!strcmp(k, "--delay-spread")) {
            c.delay_spread_ns = atof(v); i++;
        } else if (!strcmp(k, "--carrier-ghz")) {
            c.carrier_ghz = atof(v); i++;
        } else if (!strcmp(k, "--cqi-delay")) {
            c.cqi_delay_slots = atoi(v); i++;
        } else if (!strcmp(k, "--coreset-rb")) {
            c.coreset_rb = atoi(v); i++;
        } else if (!strcmp(k, "--coreset-sym")) {
            c.coreset_sym = atoi(v); i++;
        } else if (!strcmp(k, "--rbg-size")) {
            c.rbg_size = atoi(v); i++;
        } else if (!strcmp(k, "--layers")) {
            c.max_layers = atoi(v); i++;
        } else if (!strcmp(k, "--mimo-loss-db")) {
            c.mimo_layer_loss_db = atof(v); i++;
        } else if (!strcmp(k, "--sinr-map")) {
            c.sinr_map_lin = strcmp(v, "lin") == 0; i++;
        } else if (!strcmp(k, "--olla")) {
            c.olla = on_off(v); i++;
        } else if (!strcmp(k, "--mismatch-db")) {
            c.mismatch_db = atof(v); i++;
        } else if (!strcmp(k, "--ul-pc")) {
            c.ul_pc = on_off(v); i++;
        } else if (!strcmp(k, "--ul-loss-db")) {
            c.ul_loss_db = atof(v); i++;
        } else if (!strcmp(k, "--ul-target")) {
            c.ul_target_sinr_db = atof(v); i++;
        } else if (!strcmp(k, "--wifi-sta")) {
            c.wifi_sta = atoi(v); i++;
        } else if (!strcmp(k, "--wifi-shared")) {
            c.wifi_shared_band = on_off(v); i++;
        } else if (!strcmp(k, "--lte-prb")) {
            c.lte_prb = atoi(v); i++;
        } else if (!strcmp(k, "--slot-ms")) {
            c.slot_ms = atof(v); i++;
        } else if (!strcmp(k, "--phy")) {
            c.phy.enable = on_off(v); i++;
        } else if (!strcmp(k, "--phy-prb")) {
            c.phy.max_prb = atoi(v); i++;
        } else if (!strcmp(k, "--phy-bits")) {
            c.phy.target_coded = atoi(v); i++;
        } else if (!strcmp(k, "--phy-ce")) {
            c.phy.ce_mode = atoi(v); i++;
        } else if (!strcmp(k, "--result")) {
            result_path = v; i++;
        } else {
            fprintf(stderr, "未知参数: %s\n", k);
            return 1;
        }
    }

    wl_init(&g_state, &c);
    wl_run(&g_state);
    wl_result(&g_state, words);

    secs = (double)c.slots * c.slot_ms / 1000.0;
    if (secs <= 0.0) secs = 1e-9;

    nr_dl_mbps = (double)rd64(words, WL_R_NR_DL_BITS_LO) / secs / 1e6;
    nr_ul_mbps = (double)rd64(words, WL_R_NR_UL_BITS_LO) / secs / 1e6;
    lte_mbps = (double)rd64(words, WL_R_LTE_BITS_LO) / secs / 1e6;
    wifi_mbps = (double)rd64(words, WL_R_WIFI_BITS_LO) / secs / 1e6;
    nack = (double)words[WL_R_NR_NACK_PPM] / 10000.0;
    first_bler = (double)words[WL_R_NR_FIRST_BLER_PPM] / 10000.0;
    jain = (double)words[WL_R_JAIN_PPM] / 1000000.0;
    fs_gain = (double)(int32_t)words[WL_R_FS_GAIN_PPM] / 10000.0;
    cce_util = (double)words[WL_R_CCE_UTIL_PPM] / 10000.0;

    if (!quiet) {
        printf("wlbench v%u · %s · %d slots (%g ms) · %d UE · seed %d\n",
               (unsigned)WL_SCHEMA, c.scenario_mixed ? "mixed" : "nr-only",
               c.slots, (double)c.slots * c.slot_ms, c.n_ue, c.seed);
        printf("NR   DL %.2f Mb/s  UL %.2f Mb/s  TB %u  NACK %.1f%% (首传 %.1f%%)  重传 %u\n",
               nr_dl_mbps, nr_ul_mbps,
               (unsigned)words[WL_R_NR_DL_TB], nack, first_bler,
               (unsigned)words[WL_R_NR_DL_RETX]);
        printf("     PDCCH %u  SSB %u  SIB1 %u  PRACH %u (成功 %u / 失败 %u)\n",
               (unsigned)words[WL_R_NR_PDCCH], (unsigned)words[WL_R_NR_SSB],
               (unsigned)words[WL_R_NR_SIB1], (unsigned)words[WL_R_NR_PRACH],
               (unsigned)words[WL_R_NR_RA_OK], (unsigned)words[WL_R_NR_RA_FAIL]);
        printf("     队列时延 %.2f slot (最大 %.2f)  公平性 J=%.3f  频选增益 %+.2f%%  CCE 占用 %.1f%%\n",
               (double)words[WL_R_LAT_MEAN_MSLOT] / 1000.0,
               (double)words[WL_R_LAT_MAX_MSLOT] / 1000.0,
               jain, fs_gain, cce_util);
        printf("     平均 %.2f 层  OLLA %+.2f dB  信道起伏 %.1f dB\n",
               (double)words[WL_R_LAYERS_MEAN_MILLI] / 1000.0,
               (double)(int32_t)words[WL_R_OLLA_MEAN_MDB] / 1000.0,
               (double)(int32_t)words[WL_R_SPREAD_MDB] / 1000.0);
        printf("UL   功控平均偏置 %+.2f dB  gNB 实测 SINR %.2f dB\n",
               (double)(int32_t)words[WL_R_UL_POWER_MEAN_MDB] / 1000.0,
               (double)(int32_t)words[WL_R_UL_SINR_MEAN_MDB] / 1000.0);
        printf("LTE  DL %.2f Mb/s  TB %u  NACK %.1f%%\n",
               lte_mbps, (unsigned)words[WL_R_LTE_TB],
               words[WL_R_LTE_TB] > 0
                   ? (double)words[WL_R_LTE_NACK] / (double)words[WL_R_LTE_TB] * 100.0
                   : 0.0);
        printf("WiFi %.2f Mb/s  尝试 %u  碰撞 %u\n",
               wifi_mbps, (unsigned)words[WL_R_WIFI_ATT],
               (unsigned)words[WL_R_WIFI_COLL]);
        if (c.phy.enable) {
            printf("PHY  链路 %u 条  BLER %.1f%%  解调 %.2e -> 译码 %.2e\n",
                   (unsigned)words[WL_R_PHY_LINKS],
                   (double)words[WL_R_PHY_BLER_PPM] / 10000.0,
                   (double)words[WL_R_PHY_RAW_BER_PPM] / 1e6,
                   (double)words[WL_R_PHY_POST_BER_PPM] / 1e6);
            printf("     EVM %.2f dB  CE-NMSE %.2f dB  估计 SNR %.2f dB  码率 %.3f  PRB %.1f\n",
                   (double)(int32_t)words[WL_R_PHY_EVM_MDB] / 1000.0,
                   (double)(int32_t)words[WL_R_PHY_NMSE_MDB] / 1000.0,
                   (double)(int32_t)words[WL_R_PHY_SNR_EST_MDB] / 1000.0,
                   (double)words[WL_R_PHY_RATE_MILLI] / 1000.0,
                   (double)words[WL_R_PHY_PRB_MILLI] / 1000.0);
        }
        printf("result words: %d  config_hash 0x%08x\n",
               WL_RESULT_WORDS, words[WL_R_CONFIG_HASH]);
    }

    if (result_path) {
        FILE *f = fopen(result_path, "w");
        if (!f) {
            fprintf(stderr, "无法写入 %s\n", result_path);
            return 1;
        }
        fprintf(f, "# wlbench result v%u: index value\n", (unsigned)WL_SCHEMA);
        for (i = 0; i < WL_RESULT_WORDS; i++)
            fprintf(f, "%d %u\n", i, words[i]);
        fclose(f);
    }

    return 0;
}
