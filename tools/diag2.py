import nr_pbch as P
import random

# Test 1: triangular permutation exact inverse on signed values
random.seed(3)
x=[random.randint(-9,9) for _ in range(720)]
y=P.triangular_interleave(x)
z=P.triangular_deinterleave(y)
print("triangular inverse exact:", x==z)

# Test 2: full rate-match/recover on LLRs (no scramble, no noise)
d=[random.randint(0,1) for _ in range(512)]
e=P.rate_match_pbch(d,720)        # 720 bits
llr=[ (1 if b==0 else -1) for b in e ]   # perfect LLRs of e
rec=P.rate_recover_pbch(llr,512)         # should be perfect LLRs of d
mm=sum(1 for k in range(512) if ((1 if rec[k]>=0 else 0)!=d[k]))
print("rate-recover (no scramble) codeword mismatches:", mm, "/512")

# Test 3: same but with scramble at rate-matched level then descramble at rate-matched level before recover
scr=P.gold(601,720)
e_sc=[e[k]^scr[k] for k in range(720)]
llr2=[ (1 if (e_sc[k]^scr[k])==0 else -1) for k in range(720) ]  # descrambled -> d-equiv
rec2=P.rate_recover_pbch(llr2,512)
mm2=sum(1 for k in range(512) if ((1 if rec2[k]>=0 else 0)!=d[k]))
print("rate-recover (scrambled,descrambled) mismatches:", mm2, "/512")

# Test 4: inspect circular_recover behavior for repetition region
z=P.triangular_deinterleave(P.rate_match_pbch(d,720))
cr=P.circular_recover(z,512)
# count how many positions got 2 contributions
n=512; E=720; reps=[i for i in range(n) if E>i+ (E//n)*n ]  # positions appearing twice
print("positions appearing twice:", len([i for i in range(n) if E>=i+n]))
