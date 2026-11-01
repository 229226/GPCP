from ssb_plan import _fft
N=4096
freq=[0j]*N
freq[2023]=complex(2000,-2000)
sig=_fft(freq,True)
samples=[complex(round(c.real)*256, round(c.imag)*256) for c in sig]
rec=_fft(samples,False)
print("rec[2023] =", rec[2023])
print("expected 256*grid =", 256*complex(2000,-2000))
print("ratio rec/(256*grid) =", rec[2023]/(256*complex(2000,-2000)))
# also check max abs of rec to see if energy leaked
mx=max(abs(x) for x in rec)
print("max |rec| =", mx, "at bin", [i for i,v in enumerate(rec) if abs(v)==mx][:3])
