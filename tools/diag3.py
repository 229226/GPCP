import nr_pbch as P
import random
random.seed(3)
d=[random.randint(0,1) for _ in range(512)]

def recovery_err(d, E, scramble=False):
    e=P.rate_match_pbch(d,E)
    if scramble:
        scr=P.gold(601,E)
        e=[e[k]^scr[k] for k in range(E)]
    # perfect LLRs: sign + -> bit0 (value 0), sign - -> bit1 (value1)
    llr=[ (1 if b==0 else -1) for b in e ]
    if scramble:
        scr2=P.gold(601,E)
        llr=[ llr[k]*(-1 if scr2[k] else 1) for k in range(E) ]
    rec=P.rate_recover_pbch(llr,512)
    # recover bit from LLR: rec>=0 -> bit 0
    rec_bit=[ 0 if r>=0 else 1 for r in rec ]
    mm=[ i for i in range(512) if rec_bit[i]!=d[i] ]
    return len(mm), mm

# no scramble
n1,pos1=recovery_err(d,720,False)
print("no-scramble, E=720:", n1, "mismatch positions sample:", pos1[:10], "region<208 count:", sum(1 for p in pos1 if p<208))
# scramble
n2,pos2=recovery_err(d,720,True)
print("scrambled, E=720:", n2, "region<208 count:", sum(1 for p in pos2 if p<208))
# E < N (puncturing, no repetition)
n3,pos3=recovery_err(d,400,False)
print("no-scramble, E=400 (puncture):", n3)
