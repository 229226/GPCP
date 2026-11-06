#include <stdio.h>
#define NB 16          /* 16 butterflies per iteration */
int xr[2*NB], xi[2*NB], wr[NB], wi[NB], yr[2*NB], yi[2*NB];

__attribute__((noinline))
void gpcp_fft_bfly(){
  for (int b = 0; b < NB; b++) {
#ifdef CGRA_COMPILER
please_map_me();
#endif
    int tr = wr[b] * xr[2*b+1] - wi[b] * xi[2*b+1];
    int ti = wr[b] * xi[2*b+1] + wi[b] * xr[2*b+1];
    yr[2*b]   = xr[2*b] + tr;
    yi[2*b]   = xi[2*b] + ti;
    yr[2*b+1] = xr[2*b] - tr;
    yi[2*b+1] = xi[2*b] - ti;
  }
}

int main(){
  int i, s = 0;
  for (i = 0; i < 2*NB; i++) { xr[i] = (i*3)%9 - 4; xi[i] = (i*5)%7 - 3; }
  for (i = 0; i < NB; i++) { wr[i] = 8 - i; wi[i] = i - 4; }
  gpcp_fft_bfly();
  for (i = 0; i < 2*NB; i++) s += yr[i] + yi[i];
  printf("%d\n", s);
  return 0;
}
