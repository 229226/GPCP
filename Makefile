# GPCP top-level convenience targets

.PHONY: all test build clean sw-check soc soc-run soc-run-dma soc-run-irq soc-run-ring linux

all: test

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

# B3: OpenSBI + Linux on the SoC
linux:
	$(MAKE) -C sim/soc linux

clean:
	rm -rf build sim/cgra/build sim/soc/build
