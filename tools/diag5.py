import nr_pbch as P
from ssb_plan import Lcg
import ssb_plan_p1 as M

nid1,nid2=M.TRUE_NID1,M.TRUE_NID2
payload=M.payload_bits(0x50B0A5)
td,grid,payload,e_sc=M.build_td(nid1,nid2,payload=payload)
y=M.transmit(td,Lcg(0x50B0A5))

s=1
r0=0
useful=y[s][M.CP_LEN:M.CP_LEN+M.NFFT]
spec=M._fft(useful,inverse=False)
yw=spec[M.bin_of(r0)]
print("grid DMRS sym1 r0:", grid[s][r0])
print("yw[sym1,r0]:", yw)
print("ratio yw/grid:", (yw/grid[s][r0]) if grid[s][r0]!=0 else None)
print("expected yw/grid ~ 256*h =", 256*complex(M.CH_RE,M.CH_IM))

ci=P.pbch_dmrs_cinit(nid1,nid2,M.I_SSB,M.N_HF)
dr=M.dmrs_values(ci)
den=M.NPILOT*2*M.QPSK*M.QPSK//M.H_SCALE
spec_s=[M._fft(y[s2][M.CP_LEN:M.CP_LEN+M.NFFT],inverse=False) for s2 in range(M.NSYM)]
acc=0j; p=0
for r in range(0,M.NRE,M.DMRS_STEP):
    if r==M.DC_RE: continue
    dd=dr[p]; p+=1
    acc+=spec_s[s][M.bin_of(r)]*complex(dd.real,-dd.imag)
print("acc:",acc," den:",den," acc//den:",complex(int(acc.real//den),int(acc.imag//den)))
print("|dr|^2 per pilot:", abs(dr[0])**2)
