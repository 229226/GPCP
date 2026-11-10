/* IEEE 802.11be EHT SU 20 MHz（MCS 0）的算子注册表。 */
#include "wifi_stages.inc"

static const proto_op_t proto_ops[] = {
    { "received",        0, wifi_received },
    { "dmrs",            0, wifi_dmrs },
    { "ofdm_demodulate", "dft256_scalar", wifi_ofdm_demodulate },
    { "ofdm_demodulate", "dft256_radix2", wifi_ofdm_demodulate_fft },
    { "estimate",        0, wifi_estimate },
    { "equalise_demap",  0, wifi_equalise_demap },
    { "decode",          0, wifi_decode },
};

#define N_OPS (sizeof(proto_ops) / sizeof(proto_ops[0]))

static int proto_setup(const uint32_t *par) { return wifi_setup(par); }
#define PROTO_DMA_FIELD dmas

static void write_anchors(uint32_t *o)
{
    o[0] = wifi_sig_ok;
    o[1] = wifi_data_ok;
    o[2] = WIFI_SIG_INFO;
    o[3] = WIFI_DATA_INFO;
    o[4] = wifi_iters;
    o[5] = wifi_syn;
    o[6] = wifi_sig[0];
    o[7] = wifi_data[0];
    for (int i = 0; i < WIFI_SIG_INFO; i++)
        o[8 + i] = wifi_sig[i];
    for (int i = 0; i < WIFI_DATA_INFO; i++)
        o[48 + i] = wifi_data[i];
}
