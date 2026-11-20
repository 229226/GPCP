/* -----------------------------------------------------------------------------
 * 38.211 5.2.1 Gold sequence  c(n) = x1(n+NC) ^ x2(n+NC),  n = 0..length-1.
 *
 * Shared by every scrambling stage in the transmitter:
 *   PBCH  (A2) c_init = N_ID^cell
 *   PDSCH (B2) c_init = n_RNTI << 15     (38.211 7.3.1.1 with q=0, n_f=0)
 * One implementation, so a bit-order fix cannot land in one bench and miss the
 * other.  The caller defines NC (1600) before including this file.
 * ----------------------------------------------------------------------------- */
#ifndef GOLD_SEQ_INC
#define GOLD_SEQ_INC

#ifndef NC
#define NC 1600u
#endif

#define GOLD_MAX_LEN 1024u

/* Writes `length` scrambling bits into out[].  length must be <= GOLD_MAX_LEN
 * (the largest sequence in this project is the 720-bit PDSCH codeword). */
static void gold_seq(uint32_t c_init, uint32_t length, uint8_t *out)
{
    static uint8_t x1[NC + GOLD_MAX_LEN + 64u];
    static uint8_t x2[NC + GOLD_MAX_LEN + 64u];
    uint32_t n, i;

    if (length > GOLD_MAX_LEN)
        return;                 /* caller bug: never silently truncate */
    n = length + NC;
    for (i = 0; i < n + 31u; i++) {
        x1[i] = 0;
        x2[i] = 0;
    }
    x1[0] = 1;
    for (i = 0; i < 31u; i++)
        x2[i] = (uint8_t)((c_init >> i) & 1u);
    for (i = 0; i < n; i++) {
        x1[i + 31u] = (uint8_t)((x1[i + 3u] + x1[i]) & 1u);
        x2[i + 31u] = (uint8_t)((x2[i + 3u] + x2[i + 2u] +
                                 x2[i + 1u] + x2[i]) & 1u);
    }
    for (i = 0; i < length; i++)
        out[i] = (uint8_t)(x1[NC + i] ^ x2[NC + i]);
}

#endif /* GOLD_SEQ_INC */
