import nr_pbch as P
import random
random.seed(11)
e=[random.randint(0,1) for _ in range(720)]
ref=P.rate_recover_pbch(e,512)

z=P.triangular_deinterleave(e)
pre=P.circular_recover(z,512)
d_check=P.subblock_deinterleave(pre)
print("decompose step match:", d_check==ref)

# Now verify my tp/sb give same as decomposition
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
ellr=[(1 if b==0 else -1) for b in e]
z2=[ellr[tp[m]] for m in range(720)]
pre2=[0]*512
for k in range(720): pre2[k%512]+=z2[k]
got=[0 if pre2[sb[i]]>=0 else 1 for i in range(512)]
print("tp/sb match ref(bits):", got==ref)
# find first diff
for i in range(512):
    if got[i]!=ref[i]:
        print("first diff at",i,"got",got[i],"ref",ref[i],"pre2",pre2[sb[i]],"pre",pre[sb[i]])
        break
