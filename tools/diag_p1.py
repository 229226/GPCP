import nr_seq, nr_pbch as P
from ssb_plan import (bitrev, is_frozen, info_indices, polar_encode, sc_decode)
import ssb_plan_p1 as M

NSYM=4; NRE=240; QPSK=2000; LLR_SHIFT=2; H_SCALE=16
nid1=M.TRUE_NID1; nid2=M.TRUE_NID2
# 1) build a codeword d and its rate-matched scrambled bits
import random
random.seed(7)
payload=[random.randint(0,1) for _ in range(32)]
ufull=[0]*512
for k,i in enumerate(info_indices()): ufull[i]=payload[k]
d=polar_encode(ufull)
E=720
e=P.rate_match_pbch(d,E)
scr=P.gold(P.nid_cell(nid1,nid2),E)
e_sc=[e[b]^scr[b] for b in range(E)]

# 2) QPSK symbols for e_sc, equalize with h=(2,1), form LLRs
h=complex(2,1)
def qpsk(b0,b1): return complex(1-2*b0,1-2*b1)*QPSK
llr=[0]*E
for m in range(E//2):
    b0=e_sc[2*m]; b1=e_sc[2*m+1]
    sym=qpsk(b0,b1)*h
    v0=int(sym.real)>>LLR_SHIFT
    v1=int(sym.imag)>>LLR_SHIFT
    llr[2*m] = -v0 if scr[2*m] else v0
    llr[2*m+1] = -v1 if scr[2*m+1] else v1
z=P.rate_recover_pbch(llr,512)
# compare sign(z[k]) to d[k] (LLR>0 => bit 0)
mm=sum(1 for k in range(512) if ((1 if z[k]>=0 else 0)!=d[k]))
print("codeword LLR vs d mismatches:", mm, "/512")

# 3) Polar round-trip alone (no rate matching)
llr_d=[ 1 if bit==0 else -1 for bit in d]  # perfect LLRs
dec=sc_decode(llr_d)
rec=[dec[i] for i in info_indices()]
print("polar round-trip payload mismatches:", sum(1 for a,b in zip(rec,payload) if a!=b))

# 4) full receiver on noiseless
rx,pl=M.run_one()
print("full noiseless MIB mismatches:", sum(1 for a,b in zip(rx,pl) if a!=b),"/32")
print("mismatch positions:", [i for i,(a,b) in enumerate(zip(rx,pl)) if a!=b])
