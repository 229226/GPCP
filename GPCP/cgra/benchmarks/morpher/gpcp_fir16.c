#include <stdio.h>
#define N 64
#define TAPS 16
int x[N + TAPS], h[TAPS], y[N];

/* Mapped loop is the inner tap loop: per iteration 2 loads + 1 MAC.
   Loop-carried dependence on acc is the recurrence we want Morpher to price. */
__attribute__((noinline))
void gpcp_fir16(){
  for (int n = 0; n < N; n++) {
    int acc = 0;
    for (int t = 0; t < TAPS; t++) {
#ifdef CGRA_COMPILER
please_map_me();
#endif
      acc += h[t] * x[n + t];
    }
    y[n] = acc;
  }
}

int main(){
  int i, s = 0;
  for (i = 0; i < N + TAPS; i++) x[i] = (i * 5) % 13 - 6;
  for (i = 0; i < TAPS; i++) h[i] = i - 7;
  gpcp_fir16();
  for (i = 0; i < N; i++) s += y[i];
  printf("%d\n", s);
  return 0;
}
