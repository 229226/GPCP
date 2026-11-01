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
tp=tri_perm(); sb=sb_perm()
random.seed(3)
# test triangular permutation
e=[random.randint(0,1) for _ in range(720)]
z=P.triangular_deinterleave(e)
z2=[e[tp[m]] for m in range(720)]
print("tri_perm correct:", z==z2)
# test subblock permutation
y=[random.randint(0,1) for _ in range(512)]
d=P.subblock_deinterleave(y)
d2=[y[sb[i]] for i in range(512)]
print("sb_perm correct:", d==d2)
