import nr_pbch as P
from ssb_plan import (info_indices, polar_encode)
import ssb_plan_p1 as M

nid1,nid2=M.TRUE_NID1,M.TRUE_NID2
payload=M.payload_bits(0x50B0A5)
ufull=[0]*512
for k,i in enumerate(info_indices()): ufull[i]=payload[k]
d=polar_encode(ufull)

td,grid,payload,e_sc=M.build_td(nid1,nid2,payload=payload)
# NOISELESS channel only: y = td * h
h=complex(M.CH_RE,M.CH_IM)
y=[[td[s][k]*h for k in range(len(td[s]))] for s in range(M.NSYM)]

spec_s=[M._fft(y[s][M.CP_LEN:M.CP_LEN+M.NFFT],inverse=False) for s in range(M.NSYM)]
# check one DMRS bin round-trip scaling
yw0=spec_s[1][M.bin_of(0)]
print("noiseless yw[1,0]/grid =", yw0/grid[1][0])
print("expected const ~ 256*1.27*h =", 256*1.27*h)

# full noiseless receiver
yw=[[0j]*M.NRE for _ in range(M.NSYM)]
for s in range(M.NSYM):
    for r in range(M.NRE):
        if r==M.DC_RE: continue
        yw[s][r]=spec_s[s][M.bin_of(r)]
ci=P.pbch_dmrs_cinit(nid1,nid2,M.I_SSB,M.N_HF)
dr=M.dmrs_values(ci)
hs=[]; den=M.NPILOT*2*M.QPSK*M.QPSK//M.H_SCALE
p=0
for s in M.PBCH_SYMS:
    acc=0j
    for r in range(0,M.NRE,M.DMRS_STEP):
        if r==M.DC_RE: continue
        dd=dr[p]; p+=1
        acc+=yw[s][r]*complex(dd.real,-dd.imag)
    hs.append(complex(int(acc.real//den),int(acc.imag//den)))
scr=P.gold(P.nid_cell(nid1,nid2),M.E)
llr=[0]*M.E
dl=M.pbch_data_list()
for m in range(len(dl)):
    s,r=dl[m]; hc=hs[M.PBCH_SYMS.index(s)]
    eq=yw[s][r]*complex(hc.real,-hc.imag)
    for bit,soft in ((0,eq.real),(1,eq.imag)):
        v=max(-2000000,min(2000000,int(soft)>>M.LLR_SHIFT))
        llr[2*m+bit]=-v if scr[2*m+bit] else v
z=P.rate_recover_pbch(llr,512)
rec=[0 if zz>=0 else 1 for zz in z]
print("noiseless codeword sign vs d mismatches:", sum(1 for i in range(512) if rec[i]!=d[i]),"/512")
