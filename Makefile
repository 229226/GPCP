# GPCP top-level convenience targets

.PHONY: all test build clean verify verify-full sw-check soc soc-run soc-run-dma soc-run-irq soc-run-ring soc-run-rxchain soc-run-fft soc-run-sync soc-run-sss soc-run-polar soc-run-ssb soc-run-ssbfft soc-run-fftprobe soc-run-slot soc-run-ssb-slot linux

all: test

# One-command acceptance: kernels + a quick SoC subset
verify:
	bash tools/verify.sh quick

# Full SoC benchmark matrix
verify-full:
	bash tools/verify.sh full

# Build the Verilator model (if needed) and run all CGRA kernel tests
test:
	python3 tools/cgra.py test --rebuild

build:
	python3 tools/cgra.py build

sw-check:
	$(MAKE) -C sw all
	$(MAKE) -C sw rv64

# B2: CVA6 + CGRA SoC
soc:
	$(MAKE) -C sim/soc

soc-run:
	$(MAKE) -C sim/soc run

# L1 data plane: CVA6 drives the copy engine (DMA + double buffering)
soc-run-dma:
	$(MAKE) -C sim/soc run-dma

# L1 data plane: interrupt-driven variant (CGRA -> PLIC -> CVA6 + wfi)
soc-run-irq:
	$(MAKE) -C sim/soc run-irq

# L1 data plane: descriptor ring + burst copy
soc-run-ring:
	$(MAKE) -C sim/soc run-ring

# L1 receive chain: chnest -> MMSE -> LDPC-CN over the ring + double buffering
soc-run-rxchain:
	$(MAKE) -C sim/soc run-rxchain

# FFT: tiled radix-4 stages + fused radix-16 tail over the descriptor ring
soc-run-fft:
	$(MAKE) -C sim/soc run-fft

# Slot time base: CLINT timer drives a slot loop (PLIC + wfi)
soc-run-sync: ; $(MAKE) -C sim/soc run-sync

soc-run-sss: ; $(MAKE) -C sim/soc run-sss

soc-run-polar: ; $(MAKE) -C sim/soc run-polar

soc-run-ssb: ; $(MAKE) -C sim/soc run-ssb

soc-run-ssbfft: ; $(MAKE) -C sim/soc run-ssbfft
soc-run-ssb-slot: ; $(MAKE) -C sim/soc run-ssb-slot
soc-run-fftprobe: ; $(MAKE) -C sim/soc run-fftprobe

soc-run-slot:
	$(MAKE) -C sim/soc run-slot

# B3: OpenSBI + Linux on the SoC
linux:
	$(MAKE) -C sim/soc linux

clean:
	rm -rf build sim/cgra/build sim/soc/build
