import nr_pbch as P
import random
def tri_perm(E=720):
    t=P._tri_t(E); cells=P._tri_cells(t,E); cset=set(cells); colidx={}; idx=0
    for j in range(t):
        for i in range(t):
            if (i,j) in cset: colidx[(i,j)]=idx; idx+=1
    return [colidx[cells[k]] for k in range(E)]
def sb_perm(N=512):
    c=32; r=N//c; pidx={p:i for i,p in enumerate(P.SUBBLK_PERM)}; out=[]
    for k in range(N):
        ri=k//c; ci=k%c; out.append(pidx[ci]*r+ri)
    return out
tp=tri_perm(720); sb=sb_perm(512)
random.seed(11)
bad=0
for _ in range(50):
    d=[random.randint(0,1) for _ in range(512)]
    e=P.rate_match_pbch(d,720)               # valid rate-matched sequence
    ref=P.rate_recover_pbch(e,512)          # recovered codeword bits
    ellr=[(1 if b==0 else -1) for b in e]
    z=[ellr[tp[m]] for m in range(720)]
    pre=[0]*512
    for k in range(720): pre[k%512]+=z[k]
    got=[0 if pre[sb[i]]>=0 else 1 for i in range(512)]
    if got!=ref: bad+=1
print("LUT round-trip on valid rate-matched seqs: bad =",bad,"/50")
