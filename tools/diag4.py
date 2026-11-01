import nr_seq
import nr_pbch as P
from ssb_plan import (bitrev, is_frozen, info_indices, polar_encode)
import ssb_plan_p1 as M

payload = M.payload_bits(0x50B0A5)
nid1, nid2 = M.TRUE_NID1, M.TRUE_NID2
ufull=[0]*512
for k,i in enumerate(info_indices()): ufull[i]=payload[k]
d = polar_encode(ufull)

# build grid + td + y, noiseless (seed=0 -> noise? Lcg uses seed)
import ssb_plan
td, grid, payload, e_sc = M.build_td(nid1, nid2, payload=payload)
lcg = ssb_plan.Lcg(0x50B0A5)
y = M.transmit(td, lcg)

# mirror receiver up to rate recover
spec=[]; NSYM=M.NSYM; NRE=M.NRE; CP=M.CP_LEN; NFFT=M.NFFT
for s in range(NSYM):
    useful=y[s][CP:CP+NFFT]
    spec.append(M._fft(useful, inverse=False))
yw=[[0j]*NRE for _ in range(NSYM)]
for s in range(NSYM):
    for r in range(NRE):
        if r==M.DC_RE: continue
        yw[s][r]=spec[s][M.bin_of(r)]
ci=P.pbch_dmrs_cinit(nid1,nid2,M.I_SSB,M.N_HF)
dr=M.dmrs_values(ci)
h=[]; den=M.NPILOT*2*M.QPSK*M.QPSK//M.H_SCALE
p=0
for s in M.PBCH_SYMS:
    acc=0j
    for r in range(0,NRE,M.DMRS_STEP):
        if r==M.DC_RE: continue
        dd=dr[p]; p+=1
        acc+=yw[s][r]*complex(dd.real,-dd.imag)
    h.append(complex(int(acc.real//den),int(acc.imag//den)))
scr=P.gold(P.nid_cell(nid1,nid2), M.E)
llr=[0]*M.E
dl=M.pbch_data_list()
for m in range(len(dl)):
    s,r=dl[m]
    hs=h[M.PBCH_SYMS.index(s)]
    eq=yw[s][r]*complex(hs.real,-hs.imag)
    for bit,soft in ((0,eq.real),(1,eq.imag)):
        v=int(soft)>>M.LLR_SHIFT
        v=max(-2000000,min(2000000,v))
        llr[2*m+bit] = -v if scr[2*m+bit] else v
z=P.rate_recover_pbch(llr,512)
# compare recovered codeword sign to d
rec_bit=[0 if zz>=0 else 1 for zz in z]
mm=[i for i in range(512) if rec_bit[i]!=d[i]]
print("codeword sign vs d mismatches:", len(mm), "/512")
print("region<208:", sum(1 for i in mm if i<208))
# also check channel estimate
print("h_est:", h, "true h*16:", complex(M.CH_RE*M.H_SCALE, M.CH_IM*M.H_SCALE))
