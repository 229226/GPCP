#include <stdio.h>
#define M 4
int A[M*M], B[M*M], C[M*M];

/* Polybench-shaped GEMM: mapped loop is k, one MAC per iteration. */
__attribute__((noinline))
void gpcp_gemm4(){
  for (int i = 0; i < M; i++)
    for (int j = 0; j < M; j++)
      for (int k = 0; k < M; k++) {
#ifdef CGRA_COMPILER
please_map_me();
#endif
        C[i*M + j] += A[i*M + k] * B[k*M + j];
      }
}

int main(){
  int i, s = 0;
  for (i = 0; i < M*M; i++) { A[i] = i % 7 - 3; B[i] = (i * 3) % 11 - 5; C[i] = 0; }
  gpcp_gemm4();
  for (i = 0; i < M*M; i++) s += C[i];
  printf("%d\n", s);
  return 0;
}
