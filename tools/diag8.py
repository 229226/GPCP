import ssb_plan_p1 as M
for snr in (-30,-20,-10,-5,0,5,10):
    e=t=0
    for _ in range(20):
        rx,pl=M.run_one(snr_db=snr)
        e+=sum(1 for a,b in zip(rx,pl) if a!=b); t+=len(pl)
    print(f"SNR={snr:>4}dB  BER={e/t:.4f}  ({e}/{t})")
# print actual noise std used
import math, random
td,_,_,_=M.build_td(payload=M.payload_bits(1))
sq=sum(abs(td[s][k])**2 for s in range(M.NSYM) for k in range(len(td[s])))
cnt=sum(len(td[s]) for s in range(M.NSYM))
sig_rms=math.sqrt(sq/cnt)
print("sig_rms(time)=",sig_rms," nstd@-5dB=",sig_rms/10**(-5/20)," a=",sig_rms/10**(-5/20)*math.sqrt(3))
print("sample TD magnitude:", abs(td[0][100]))
