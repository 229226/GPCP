# Verilator model of the GPCP SoC (CVA6 + CGRA), milestone B2/B3
#
# NOTE: --threads must stay 1.  With --threads >1 this design (CVA6 + our AXI
# slave) miscompiles under Verilator 5.035: OpenSBI/Kernel hangs at random
# points (e.g. _start_hang / fw_platform_init fail path) even though the RTL
# is correct.  Verified by rebuilding with --threads 1.
#
#   make          build sim/soc/build/Vgpcp_soc (this takes a while: CVA6 is large)
#   make run      run the bare-metal CGRA test and compare results
#   make clean

SHELL     := /bin/bash          # recipes use bash (set -o pipefail in `linux`)
ROOT      := ../..
OBJ       := sim/soc/build
BIN       := $(ROOT)/$(OBJ)/Vgpcp_soc
# Prefer the Verilator that ships with this project (third_party/eda, fetched by
# tools/fetch_eda.sh); fall back to PATH if it is not installed.
LOCAL_VERILATOR := $(wildcard $(CURDIR)/../../third_party/eda/oss-cad-suite/bin/verilator)
VERILATOR ?= $(if $(LOCAL_VERILATOR),$(LOCAL_VERILATOR),verilator)

INC := \
	-Ithird_party/cva6/core/include \
	-Ithird_party/cva6/vendor/pulp-platform/common_cells/include \
	-Ithird_party/cva6/vendor/pulp-platform/axi/include \
	-Ithird_party/cva6/corev_apu/register_interface/include \
	-Ithird_party/cva6/core/cvfpu/src \
	-Ithird_party/cva6/corev_apu/tb

SRCS := rtl/cgra/cgra_pkg.sv \
	rtl/cgra/cgra_pe.sv \
	rtl/cgra/cgra_array.sv \
	rtl/cgra/cgra_top.sv \
	rtl/soc/gpcp_soc.sv \
	sim/soc/tb_soc.cpp

VFLAGS := --cc --exe --build -O3 --build-jobs 4 --threads 1 \
	--top-module gpcp_soc \
	-Wno-fatal -Wno-BLKANDNBLK -Wno-UNOPTFLAT -Wno-WIDTH -Wno-WIDTHTRUNC \
	-Wno-WIDTHEXPAND -Wno-CASEINCOMPLETE -Wno-LATCH -Wno-UNUSEDSIGNAL \
	-Wno-UNUSEDPARAM -Wno-DECLFILENAME -Wno-INITIALDLY

.PHONY: all run run-dma run-irq run-ring run-rxchain run-fft run-sync run-sss run-polar run-ssb run-ssbfft run-fftprobe run-slot run-ssb-slot linux clean run-ssb-fid run-pdsch-full run-pdsch-harq run-pdsch-16qam run-pdsch-64qam run-pdcch run-link-e2e run-pusch run-pusch-tp run-sib1 run-pucch run-csi run-mimo-ce run-mimo run-prach run-prach-cgra run-rach

# The model is only rebuilt when one of its inputs changes; `make all` used to
# re-run Verilator every time (~35 s), which made the run-* targets painful to
# chain.  Header/f-list changes inside CVA6 are not tracked: `make clean` if you
# touch third_party/cva6.
# Prerequisites are written relative to the repo root (that is where the
# recipes cd into), so they must be prefixed for make, which runs in sim/soc.
PREREQ := $(addprefix $(ROOT)/,$(SRCS)) $(ROOT)/sim/soc/cva6.f

all: $(BIN)

$(BIN): $(PREREQ)
	@mkdir -p $(ROOT)/$(OBJ)
	cd $(ROOT) && env -u LD_LIBRARY_PATH $(VERILATOR) $(VFLAGS) $(INC) \
		-f sim/soc/cva6.f $(SRCS) --Mdir $(OBJ) -o Vgpcp_soc

run: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	env -u LD_LIBRARY_PATH $(BIN) --load $(ROOT)/build/sw/cgra_bm.bin@0x80000000 --results 0x80002000 --nwords 24 \
		--dump $(ROOT)/$(OBJ)/soc_dump.txt
	python3 $(ROOT)/tools/check_soc.py $(ROOT)/$(OBJ)/soc_dump.txt

# DMA + double buffering: CVA6 streams three slots through the two scratchpad
# pages while the array computes on the other page
run-dma: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	env -u LD_LIBRARY_PATH $(BIN) --load $(ROOT)/build/sw/dma_bm.bin@0x80000000 --results 0x80010000 --nwords 50 \
		--dump $(ROOT)/$(OBJ)/dma_dump.txt
	python3 $(ROOT)/tools/check_dma.py $(ROOT)/$(OBJ)/dma_dump.txt

# Interrupt-driven variant: the CPU waits in wfi and the CGRA raises a level
# interrupt through the PLIC on every copy-engine / array completion
run-irq: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	env -u LD_LIBRARY_PATH $(BIN) --load $(ROOT)/build/sw/irq_bm.bin@0x80000000 --results 0x80010000 --nwords 35 \
		--dump $(ROOT)/$(OBJ)/irq_dump.txt
	python3 $(ROOT)/tools/check_irq.py $(ROOT)/$(OBJ)/irq_dump.txt

# Descriptor ring + 64-bit burst copy: the engine reads descriptors out of RAM
run-ring: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	env -u LD_LIBRARY_PATH $(BIN) --load $(ROOT)/build/sw/ring_bm.bin@0x80000000 --results 0x80010000 --nwords 40 \
		--dump $(ROOT)/$(OBJ)/ring_dump.txt
	python3 $(ROOT)/tools/check_ring.py $(ROOT)/$(OBJ)/ring_dump.txt

# L1 receive chain: chnest -> mmse -> LDPC-CN, fused microcode per slot, ring +
# double buffering (array works one page while the engine fills/drains the other)
run-rxchain: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	env -u LD_LIBRARY_PATH $(BIN) --load $(ROOT)/build/sw/rxchain_bm.bin@0x80000000 --results 0x80010000 --nwords 101 \
		--dump $(ROOT)/$(OBJ)/rxchain_dump.txt
	python3 $(ROOT)/tools/check_rxchain.py $(ROOT)/$(OBJ)/rxchain_dump.txt

# PDSCH LDPC iteration loop: 8 variable nodes / 4 check nodes, one PGBA run per
# stage per iteration (check node 50 cycles, variable node 12 cycles)
run-pdsch: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	env -u LD_LIBRARY_PATH $(BIN) --load $(ROOT)/build/sw/pdsch_bm.bin@0x80000000 --results 0x80010000 --nwords 14 \
		--dump $(ROOT)/$(OBJ)/pdsch_dump.txt
	python3 $(ROOT)/tools/check_pdsch.py $(ROOT)/$(OBJ)/pdsch_dump.txt

# Real BG2 block, DMA-blocked: every check node streamed in/out, 10-input
# min-sum with sentinel padding, one summary + 42x3x20 result words
run-pdsch-bg2: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	env -u LD_LIBRARY_PATH $(BIN) --load $(ROOT)/build/sw/pdsch_bg2_bm.bin@0x80000000 --results 0x80010000 --nwords 1270 \
		--dump $(ROOT)/$(OBJ)/pdsch_bg2_dump.txt
	python3 $(ROOT)/tools/check_pdsch_bg2.py $(ROOT)/$(OBJ)/pdsch_bg2_dump.txt

# Debug: one variable block through the streaming BN microcode (26 x 20 words)
run-pdsch-bn1: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	env -u LD_LIBRARY_PATH $(BIN) --load $(ROOT)/build/sw/pdsch_bn1_bm.bin@0x80000000 --results 0x80010000 --nwords 268 \
		--dump $(ROOT)/$(OBJ)/pdsch_bn1_dump.txt
	python3 $(ROOT)/tools/check_pdsch_bn1.py $(ROOT)/$(OBJ)/pdsch_bn1_dump.txt

# P1: full BG2 LDPC iteration (52 variable blocks x 8) on the generated microcode
run-pdsch-iter: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	env -u LD_LIBRARY_PATH $(BIN) --load $(ROOT)/build/sw/pdsch_iter_bm.bin@0x80000000 --results 0x80010000 --nwords 1060 \
		--dump $(ROOT)/$(OBJ)/pdsch_iter_dump.txt
	python3 $(ROOT)/tools/check_pdsch_iter.py $(ROOT)/$(OBJ)/pdsch_iter_dump.txt

# Debug: per-edge reconstruction + 24-slot gather for variable block 0
run-pdsch-gather: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	env -u LD_LIBRARY_PATH $(BIN) --load $(ROOT)/build/sw/pdsch_gather_bm.bin@0x80000000 --results 0x80010000 --nwords 2060 \
		--dump $(ROOT)/$(OBJ)/pdsch_gather_dump.txt
	python3 $(ROOT)/tools/check_pdsch_gather.py $(ROOT)/$(OBJ)/pdsch_gather_dump.txt

# Multi-stage FFT (256/1024/4096): tiled radix-4 stages over the ring plus the
# fused radix-16 tail; twiddles come from a Q15 ROM image generated by
# tools/fft_plan.py
run-fft: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	@test -f $(ROOT)/build/fft/twiddle.bin || python3 $(ROOT)/tools/fft_plan.py lut $(ROOT)/build/fft/twiddle.bin
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/fft_bm.bin@0x80000000 \
		--load $(ROOT)/build/fft/twiddle.bin@0x80100000 \
		--results 0x80010000 --nwords 10760 \
		--dump $(ROOT)/$(OBJ)/fft_dump.txt
	python3 $(ROOT)/tools/check_fft.py $(ROOT)/$(OBJ)/fft_dump.txt

# Slot-synchronous scheduler skeleton: CLINT timer ticks define the slot
# cadence, the main loop sleeps in wfi and services one slot per tick
run-sync: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/sync_bm.bin@0x80000000 \
		--results 0x80010000 --nwords 53 \
		--dump $(ROOT)/$(OBJ)/sync_dump.txt \
		--max-cycles 2000000
	python3 $(ROOT)/tools/check_sync.py $(ROOT)/$(OBJ)/sync_dump.txt

run-sss: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/sss_bm.bin@0x80000000 \
		--results 0x80010000 --nwords 41 \
		--dump $(ROOT)/$(OBJ)/sss_dump.txt \
		--max-cycles 4000000
	python3 $(ROOT)/tools/check_sss.py $(ROOT)/$(OBJ)/sss_dump.txt

run-polar: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/polar_sc_bm.bin@0x80000000 \
		--results 0x80010000 --nwords 135 \
		--dump $(ROOT)/$(OBJ)/polar_dump.txt \
		--max-cycles 4000000
	python3 $(ROOT)/tools/check_sc.py $(ROOT)/$(OBJ)/polar_dump.txt

run-ssb: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/ssb_bm.bin@0x80000000 \
		--results 0x80010000 --nwords 45 \
		--dump $(ROOT)/$(OBJ)/ssb_dump.txt \
		--max-cycles 20000000
	python3 $(ROOT)/tools/check_ssb.py $(ROOT)/$(OBJ)/ssb_dump.txt

SSBTD = $(ROOT)/build/ssb/td.bin

# the image must follow the model: changing ssb_plan.py changes the samples
$(SSBTD): $(ROOT)/tools/ssb_plan.py
	@mkdir -p $(dir $(SSBTD))
	python3 $(ROOT)/tools/ssb_plan.py dump $(SSBTD)

run-ssbfft: all $(SSBTD)
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	@test -f $(ROOT)/build/fft/twiddle.bin || python3 $(ROOT)/tools/fft_plan.py lut $(ROOT)/build/fft/twiddle.bin
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/ssb_fft_bm.bin@0x80000000 \
		--load $(SSBTD)@0x80400000 \
		--load $(ROOT)/build/fft/twiddle.bin@0x80100000 \
		--results 0x80010000 --nwords 992 \
		--dump $(ROOT)/$(OBJ)/ssb_fft_dump.txt \
		--max-cycles 40000000
	python3 $(ROOT)/tools/check_ssb_fft.py $(ROOT)/$(OBJ)/ssb_fft_dump.txt

# FFT port used by the sample-level SSB chain (ssb_fft.inc): every bin compared
# bit-exactly with the fixed-point reference for the same time-domain symbol
# P0.2: sample-level SSB chain driven by the CLINT slot timer -- the 336 SSS
# hypotheses are amortised across slot ticks (one batch per tick) instead of run
# as one straight line.  Same loads as run-ssbfft; the checker validates the MIB
# plus the slot-scheduling bookkeeping (ticks / overruns / bad traps).
run-ssb-slot: all $(SSBTD)
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	@test -f $(ROOT)/build/fft/twiddle.bin || python3 $(ROOT)/tools/fft_plan.py lut $(ROOT)/build/fft/twiddle.bin
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/ssb_slot_bm.bin@0x80000000 \
		--load $(SSBTD)@0x80400000 \
		--load $(ROOT)/build/fft/twiddle.bin@0x80100000 \
		--results 0x80010000 --nwords 1000 \
		--dump $(ROOT)/$(OBJ)/ssb_slot_dump.txt \
		--max-cycles 40000000
	python3 $(ROOT)/tools/check_ssb_slot.py $(ROOT)/$(OBJ)/ssb_slot_dump.txt

run-fftprobe: all $(SSBTD)
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	@test -f $(ROOT)/build/fft/twiddle.bin || python3 $(ROOT)/tools/fft_plan.py lut $(ROOT)/build/fft/twiddle.bin
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/fftprobe_bm.bin@0x80000000 \
		--load $(SSBTD)@0x80400000 \
		--load $(ROOT)/build/fft/twiddle.bin@0x80100000 \
		--results 0x80010000 --nwords 4097 \
		--dump $(ROOT)/$(OBJ)/fftprobe_dump.txt \
		--max-cycles 40000000
	python3 $(ROOT)/tools/check_fftprobe.py $(ROOT)/$(OBJ)/fftprobe_dump.txt

run-slot: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	env -u LD_LIBRARY_PATH $(BIN) --load $(ROOT)/build/sw/slot_rtos.bin@0x80000000 \
		--results 0x80010000 --nwords 282 \
		--dump $(ROOT)/$(OBJ)/slot_dump.txt
	python3 $(ROOT)/tools/check_slot.py $(ROOT)/$(OBJ)/slot_dump.txt

# B3: OpenSBI + Linux + initramfs
# Linux boot (B3).  The model runs at ~32 k cycles/s here, so a full boot is a
# long background job; the log is kept so tools/check_linux.py can grade (and
# re-grade) it without re-running anything.
#
#   make linux                       # default budget, then grade stage 7
#   make linux LINUX_MAX_CYCLES=1e8 LINUX_REQUIRE=3   # probe: "did init run?"
#   make linux-status                # re-grade the existing log only
LINUX_MAX_CYCLES ?= 200000000
LINUX_LOG        ?= $(ROOT)/build/linux_boot.log
LINUX_REQUIRE    ?= 7
LINUX_DTB        ?= $(ROOT)/build/linux/gpcp.dtb
# LINUX_TRACE=1000000 asks the bench to print the retired PC every N cycles
# (use with tools/sym_pc.py to see where a silent boot is spending its time)
LINUX_TRACE      ?=
LINUX_IMAGES      = --load $(ROOT)/third_party/opensbi/build/platform/generic/firmware/fw_jump.bin@0x80000000 \
                    --load $(ROOT)/third_party/linux-6.6.52/arch/riscv/boot/Image@0x80200000 \
                    --load $(LINUX_DTB)@0x81F00000 \
                    --load $(ROOT)/build/linux/initramfs.cpio@0x82000000

linux: all
	$(MAKE) -C $(ROOT)/sw/linux
	set -o pipefail; env -u LD_LIBRARY_PATH $(BIN) $(LINUX_IMAGES) \
		--max-cycles $(LINUX_MAX_CYCLES) $(if $(LINUX_TRACE),--trace-pc $(LINUX_TRACE),) \
		2>&1 | tee $(LINUX_LOG)
	python3 $(ROOT)/tools/check_linux.py $(LINUX_LOG) --require $(LINUX_REQUIRE)

linux-status:
	python3 $(ROOT)/tools/check_linux.py $(LINUX_LOG) --require $(LINUX_REQUIRE)

# Same boot with the earlycon-only DTB (the 16550 console goes silent mid-boot)
LINUX_EARLYCON := $(subst gpcp.dtb,gpcp-earlycon.dtb,$(LINUX_DTB))

linux-earlycon: all
	$(MAKE) -C $(ROOT)/sw/linux
	set -o pipefail; env -u LD_LIBRARY_PATH $(BIN) $(subst $(LINUX_DTB),$(LINUX_EARLYCON),$(LINUX_IMAGES)) \
		--max-cycles $(LINUX_MAX_CYCLES) $(if $(LINUX_TRACE),--trace-pc $(LINUX_TRACE),) \
		2>&1 | tee $(LINUX_LOG)
	python3 $(ROOT)/tools/check_linux.py $(LINUX_LOG) --require $(LINUX_REQUIRE)

clean:
	rm -rf $(ROOT)/$(OBJ)

# ---- P1 real-mapping SSB benchmark (38.211/38.212 chain on the SoC) ----
SSBFID_TD = $(ROOT)/build/soc/ssb_fid.td

$(SSBFID_TD): $(ROOT)/tools/ssb_plan_p1.py $(ROOT)/tools/nr_pbch.py
	@mkdir -p $(dir $(SSBFID_TD))
	python3 $(ROOT)/tools/ssb_plan_p1.py dump $@ $(if $(SNR),--snr $(SNR),)

run-ssb-fid: all $(SSBFID_TD)
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	@test -f $(ROOT)/build/fft/twiddle.bin || python3 $(ROOT)/tools/fft_plan.py lut $(ROOT)/build/fft/twiddle.bin
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/ssb_fid_bm.bin@0x80000000 \
		--load $(SSBFID_TD)@0x80400000 \
		--load $(ROOT)/build/fft/twiddle.bin@0x80100000 \
		--results 0x80010000 --nwords 32 \
		--dump $(ROOT)/$(OBJ)/ssb_fid_dump.txt \
		--max-cycles 100000000
	python3 $(ROOT)/tools/check_ssb_fid.py $(ROOT)/$(OBJ)/ssb_fid_dump.txt

# ---- PDSCH (SISO) full receive chain: TD samples -> TB ----
PDSCHFULL_TD = $(ROOT)/build/soc/pdsch_full.td

run-pdsch-full: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	@mkdir -p $(dir $(PDSCHFULL_TD))
	python3 $(ROOT)/tools/pdsch_full_plan.py dump $(PDSCHFULL_TD) --snr $(if $(SNR),$(SNR),20) --seed $(if $(SEED),$(SEED),1)
	@test -f $(ROOT)/build/fft/twiddle.bin || python3 $(ROOT)/tools/fft_plan.py lut $(ROOT)/build/fft/twiddle.bin
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/pdsch_full_bm.bin@0x80000000 \
		--load $(PDSCHFULL_TD)@0x80400000 \
		--load $(ROOT)/build/fft/twiddle.bin@0x80100000 \
		--results 0x80010000 --nwords 544 \
		--dump $(ROOT)/$(OBJ)/pdsch_full_dump.txt \
		--max-cycles 100000000
	python3 $(ROOT)/tools/check_pdsch_full.py $(ROOT)/$(OBJ)/pdsch_full_dump.txt --snr $(if $(SNR),$(SNR),20) --seed $(if $(SEED),$(SEED),1)

# ---- P4 MCS-driven downlink: 16QAM PDSCH (pdsch_full_bm.c -DQAM16) ----
PDSCH16_TD = $(ROOT)/build/soc/pdsch_16qam.td

run-pdsch-16qam: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	@mkdir -p $(dir $(PDSCH16_TD))
	python3 $(ROOT)/tools/nr_pdsch_amc.py dump $(PDSCH16_TD) --mcs $(if $(MCS),$(MCS),13) --rv $(if $(RV),$(RV),0) --snr $(if $(SNR),$(SNR),20) --seed $(if $(SEED),$(SEED),1)
	@test -f $(ROOT)/build/fft/twiddle.bin || python3 $(ROOT)/tools/fft_plan.py lut $(ROOT)/build/fft/twiddle.bin
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/pdsch_16qam_bm.bin@0x80000000 \
		--load $(PDSCH16_TD)@0x80400000 \
		--load $(ROOT)/build/fft/twiddle.bin@0x80100000 \
		--results 0x80070000 --nwords 544 \
		--dump $(ROOT)/$(OBJ)/pdsch_16qam_dump.txt \
		--max-cycles 100000000
	python3 $(ROOT)/tools/check_pdsch_16qam.py $(ROOT)/$(OBJ)/pdsch_16qam_dump.txt --mcs $(if $(MCS),$(MCS),13) --rv $(if $(RV),$(RV),0) --snr $(if $(SNR),$(SNR),20) --seed $(if $(SEED),$(SEED),1)

# ---- PDSCH 64QAM: same chain, Qm=6 (120 symbols on the first 120 data REs) ----
PDSCH64_TD = $(ROOT)/build/soc/pdsch_64qam.td

run-pdsch-64qam: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	@mkdir -p $(dir $(PDSCH64_TD))
	python3 $(ROOT)/tools/nr_pdsch_amc.py dump $(PDSCH64_TD) --mcs $(if $(MCS),$(MCS),20) --rv $(if $(RV),$(RV),0) --snr $(if $(SNR),$(SNR),25) --seed $(if $(SEED),$(SEED),1)
	@test -f $(ROOT)/build/fft/twiddle.bin || python3 $(ROOT)/tools/fft_plan.py lut $(ROOT)/build/fft/twiddle.bin
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/pdsch_64qam_bm.bin@0x80000000 \
		--load $(PDSCH64_TD)@0x80400000 \
		--load $(ROOT)/build/fft/twiddle.bin@0x80100000 \
		--results 0x80070000 --nwords 544 \
		--dump $(ROOT)/$(OBJ)/pdsch_64qam_dump.txt \
		--max-cycles 100000000
	python3 $(ROOT)/tools/check_pdsch_64qam.py $(ROOT)/$(OBJ)/pdsch_64qam_dump.txt --mcs $(if $(MCS),$(MCS),20) --rv $(if $(RV),$(RV),0) --snr $(if $(SNR),$(SNR),25) --seed $(if $(SEED),$(SEED),1)

# ---- PDSCH HARQ: two RV transmissions Chase-combined before LDPC ----
PDSCHHARQ_TD = $(ROOT)/build/soc/pdsch_harq.td

run-pdsch-harq: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	@mkdir -p $(dir $(PDSCHHARQ_TD))
	python3 $(ROOT)/tools/pdsch_full_plan.py dump-harq $(PDSCHHARQ_TD) --snr $(if $(SNR),$(SNR),20) --seed $(if $(SEED),$(SEED),1)
	@test -f $(ROOT)/build/fft/twiddle.bin || python3 $(ROOT)/tools/fft_plan.py lut $(ROOT)/build/fft/twiddle.bin
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/pdsch_harq_bm.bin@0x80000000 \
		--load $(PDSCHHARQ_TD)@0x80400000 \
		--load $(ROOT)/build/fft/twiddle.bin@0x80100000 \
		--results 0x80010000 --nwords 544 \
		--dump $(ROOT)/$(OBJ)/pdsch_harq_dump.txt \
		--max-cycles 100000000
	python3 $(ROOT)/tools/check_pdsch_full.py $(ROOT)/$(OBJ)/pdsch_harq_dump.txt --harq --snr $(if $(SNR),$(SNR),20) --seed $(if $(SEED),$(SEED),1)

# ---- PUSCH (SISO) full receive chain: TD samples -> TB ----
PUSCH_TD = $(ROOT)/build/soc/pusch.td

run-pusch: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	@mkdir -p $(dir $(PUSCH_TD))
	python3 $(ROOT)/tools/nr_pusch.py dump $(PUSCH_TD) --rv $(if $(RV),$(RV),0) --snr $(if $(SNR),$(SNR),20) --seed $(if $(SEED),$(SEED),1)
	@test -f $(ROOT)/build/fft/twiddle.bin || python3 $(ROOT)/tools/fft_plan.py lut $(ROOT)/build/fft/twiddle.bin
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/pusch_bm.bin@0x80000000 \
		--load $(PUSCH_TD)@0x80400000 \
		--load $(ROOT)/build/fft/twiddle.bin@0x80100000 \
		--results 0x80070000 --nwords 544 \
		--dump $(ROOT)/$(OBJ)/pusch_dump.txt \
		--max-cycles 100000000
	python3 $(ROOT)/tools/check_pusch.py $(ROOT)/$(OBJ)/pusch_dump.txt --rv $(if $(RV),$(RV),0) --snr $(if $(SNR),$(SNR),20) --seed $(if $(SEED),$(SEED),1)

# ---- PUSCH DFT-s-OFDM: 360-point IDFT deprecoding on the SoC ----
PUSCHTP_TD = $(ROOT)/build/soc/pusch_tp.td

run-pusch-tp: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	@mkdir -p $(dir $(PUSCHTP_TD))
	python3 $(ROOT)/tools/nr_pusch.py dump $(PUSCHTP_TD) --rv $(if $(RV),$(RV),0) --tp --snr $(if $(SNR),$(SNR),20) --seed $(if $(SEED),$(SEED),1)
	@test -f $(ROOT)/build/fft/twiddle.bin || python3 $(ROOT)/tools/fft_plan.py lut $(ROOT)/build/fft/twiddle.bin
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/pusch_tp_bm.bin@0x80000000 \
		--load $(PUSCHTP_TD)@0x80400000 \
		--load $(ROOT)/build/fft/twiddle.bin@0x80100000 \
		--results 0x80070000 --nwords 544 \
		--dump $(ROOT)/$(OBJ)/pusch_tp_dump.txt \
		--max-cycles 100000000
	python3 $(ROOT)/tools/check_pusch.py $(ROOT)/$(OBJ)/pusch_tp_dump.txt --rv $(if $(RV),$(RV),0) --tp --snr $(if $(SNR),$(SNR),20) --seed $(if $(SEED),$(SEED),1)

# ---- CSI-RS + CQI report (38.211 7.4.1.5 / 38.214 5.2.2.1) ----
CSI_TD = $(ROOT)/build/soc/csi.td

run-csi: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	@mkdir -p $(dir $(CSI_TD))
	python3 $(ROOT)/tools/nr_csi.py dump $(CSI_TD) --snr $(if $(SNR),$(SNR),20) --seed $(if $(SEED),$(SEED),1)
	@test -f $(ROOT)/build/fft/twiddle.bin || python3 $(ROOT)/tools/fft_plan.py lut $(ROOT)/build/fft/twiddle.bin
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/csi_bm.bin@0x80000000 \
		--load $(CSI_TD)@0x80400000 \
		--load $(ROOT)/build/fft/twiddle.bin@0x80100000 \
		--results 0x80070000 --nwords 32 \
		--dump $(ROOT)/$(OBJ)/csi_dump.txt \
		--max-cycles 100000000
	python3 $(ROOT)/tools/check_csi.py $(ROOT)/$(OBJ)/csi_dump.txt --snr $(if $(SNR),$(SNR),20) --seed $(if $(SEED),$(SEED),1)

# ---- 2x2 MIMO channel estimation (two antennas, FDM pilots) ----
MIMOCE_TD = $(ROOT)/build/soc/mimo_ce.td

run-mimo-ce: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	@mkdir -p $(dir $(MIMOCE_TD))
	python3 $(ROOT)/tools/nr_mimo_ce.py dump $(MIMOCE_TD) --snr $(if $(SNR),$(SNR),25) --seed $(if $(SEED),$(SEED),1)
	@test -f $(ROOT)/build/fft/twiddle.bin || python3 $(ROOT)/tools/fft_plan.py lut $(ROOT)/build/fft/twiddle.bin
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/mimo_ce_bm.bin@0x80000000 \
		--load $(MIMOCE_TD)@0x80400000 \
		--load $(ROOT)/build/fft/twiddle.bin@0x80100000 \
		--results 0x80070000 --nwords 16 \
		--dump $(ROOT)/$(OBJ)/mimo_ce_dump.txt \
		--max-cycles 100000000
	python3 $(ROOT)/tools/check_mimo_ce.py $(ROOT)/$(OBJ)/mimo_ce_dump.txt --snr $(if $(SNR),$(SNR),25) --seed $(if $(SEED),$(SEED),1)

# ---- 2x2 MMSE receiver: pilots -> H -> per-RE solve -> hard bits ----
MIMO_TD = $(ROOT)/build/soc/mimo.td

run-mimo: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	@mkdir -p $(dir $(MIMO_TD))
	python3 $(ROOT)/tools/nr_mimo_ce.py dump $(MIMO_TD) --snr $(if $(SNR),$(SNR),25) --seed $(if $(SEED),$(SEED),1)
	@test -f $(ROOT)/build/fft/twiddle.bin || python3 $(ROOT)/tools/fft_plan.py lut $(ROOT)/build/fft/twiddle.bin
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/mimo_bm.bin@0x80000000 \
		--load $(MIMO_TD)@0x80400000 \
		--load $(ROOT)/build/fft/twiddle.bin@0x80100000 \
		--results 0x80070000 --nwords 200 \
		--dump $(ROOT)/$(OBJ)/mimo_dump.txt \
		--max-cycles 100000000
	python3 $(ROOT)/tools/check_mimo.py $(ROOT)/$(OBJ)/mimo_dump.txt --snr $(if $(SNR),$(SNR),25) --seed $(if $(SEED),$(SEED),1)

# ---- PUCCH format 0: 1-bit SR / HARQ-ACK on the CGRA ----
PUCCH_TD = $(ROOT)/build/soc/pucch.td

run-pucch: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	@mkdir -p $(dir $(PUCCH_TD))
	python3 $(ROOT)/tools/nr_pucch.py dump $(PUCCH_TD) --ack $(if $(ACK),$(ACK),1) --snr $(if $(SNR),$(SNR),6) --seed $(if $(SEED),$(SEED),1)
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/pucch_bm.bin@0x80000000 \
		--load $(PUCCH_TD)@0x80400000 \
		--results 0x80070000 --nwords 128 \
		--dump $(ROOT)/$(OBJ)/pucch_dump.txt \
		--max-cycles 100000000
	python3 $(ROOT)/tools/check_pucch.py $(ROOT)/$(OBJ)/pucch_dump.txt --ack $(if $(ACK),$(ACK),1) --snr $(if $(SNR),$(SNR),6) --seed $(if $(SEED),$(SEED),1)

# ---- SIB1: Type0-PDCCH (SI-RNTI) + PDSCH -> SIB1 ----
SIB1_TD = $(ROOT)/build/soc/sib1.td

run-sib1: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	@mkdir -p $(dir $(SIB1_TD))
	python3 $(ROOT)/tools/nr_sib1.py dump $(SIB1_TD) --snr $(if $(SNR),$(SNR),20) --seed $(if $(SEED),$(SEED),1) --rv $(if $(RV),$(RV),0)
	@test -f $(ROOT)/build/fft/twiddle.bin || python3 $(ROOT)/tools/fft_plan.py lut $(ROOT)/build/fft/twiddle.bin
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/sib1_bm.bin@0x80000000 \
		--load $(SIB1_TD)@0x80400000 \
		--load $(ROOT)/build/fft/twiddle.bin@0x80100000 \
		--results 0x80070000 --nwords 1100 \
		--dump $(ROOT)/$(OBJ)/sib1_dump.txt \
		--max-cycles 100000000
	python3 $(ROOT)/tools/check_sib1.py $(ROOT)/$(OBJ)/sib1_dump.txt --snr $(if $(SNR),$(SNR),20) --seed $(if $(SEED),$(SEED),1) --rv $(if $(RV),$(RV),0)

# ---- PDCCH / DCI blind detection (38.211 7.3 + 38.212 7.3) ----
PDCCH_TD = $(ROOT)/build/soc/pdcch.td

run-pdcch: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	@mkdir -p $(dir $(PDCCH_TD))
	python3 $(ROOT)/tools/nr_pdcch.py dump $(PDCCH_TD) $(if $(SNR),$(SNR),12) $(if $(SEED),$(SEED),1)
	@test -f $(ROOT)/build/fft/twiddle.bin || python3 $(ROOT)/tools/fft_plan.py lut $(ROOT)/build/fft/twiddle.bin
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/pdcch_bm.bin@0x80000000 \
		--load $(PDCCH_TD)@0x80400000 \
		--load $(ROOT)/build/fft/twiddle.bin@0x80100000 \
		--results 0x80010000 --nwords 72 \
		--dump $(ROOT)/$(OBJ)/pdcch_dump.txt \
		--max-cycles 100000000
	python3 $(ROOT)/tools/check_pdcch.py $(ROOT)/$(OBJ)/pdcch_dump.txt --snr $(if $(SNR),$(SNR),12) --seed $(if $(SEED),$(SEED),1)

# ---- DCI-driven link: PDCCH (DCI) -> rv -> PDSCH circular buffer (38.211/38.212)
# One combined TD (PDCCH symbol + PDSCH symbols) at 0x8040_0000; the SoC decodes
# the DCI, reads rv out of it, and uses that rv as the PDSCH k0 offset.
LINK_E2E_TD = $(ROOT)/build/soc/link_e2e.td

run-link-e2e: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	@mkdir -p $(dir $(LINK_E2E_TD))
	python3 $(ROOT)/tools/dump_link_e2e.py $(LINK_E2E_TD) --rv $(if $(RV),$(RV),0) --snr $(if $(SNR),$(SNR),20) --seed $(if $(SEED),$(SEED),1)
	@test -f $(ROOT)/build/fft/twiddle.bin || python3 $(ROOT)/tools/fft_plan.py lut $(ROOT)/build/fft/twiddle.bin
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/link_e2e_bm.bin@0x80000000 \
		--load $(LINK_E2E_TD)@0x80400000 \
		--load $(ROOT)/build/fft/twiddle.bin@0x80100000 \
		--results 0x80070000 --nwords 1100 \
		--dump $(ROOT)/$(OBJ)/link_e2e_dump.txt \
		--max-cycles 100000000
	python3 $(ROOT)/tools/check_link_e2e.py $(ROOT)/$(OBJ)/link_e2e_dump.txt --rv $(if $(RV),$(RV),0) --snr $(if $(SNR),$(SNR),20) --seed $(if $(SEED),$(SEED),1)

# ---- PRACH Msg1: ZC cyclic-shift search -> preamble index + timing advance ----
# Stimulus is the 139-chip received preamble plus all L_RA rotated 127-tap
# references; the CGRA does one 128-cycle run per candidate shift (complex
# reference, real part in `acc`, imaginary in a register, 16-lane butterfly
# reduction inside the same program).  PRE=/TA=/SNR=/SEED= pick the point.
PRACH_TD = $(ROOT)/build/rach/prach.td

run-prach: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	@mkdir -p $(dir $(PRACH_TD))
	python3 $(ROOT)/tools/nr_rach.py dump --prach-out $(PRACH_TD) \
		--preamble $(if $(PRE),$(PRE),2) --ta $(if $(TA),$(TA),9) \
		--prach-snr $(if $(SNR),$(SNR),6) --seed $(if $(SEED),$(SEED),1)
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/prach_bm.bin@0x80000000 \
		--load $(PRACH_TD)@0x80400000 \
		--results 0x80070000 --nwords 296 \
		--dump $(ROOT)/$(OBJ)/prach_dump.txt \
		--max-cycles 100000000
	python3 $(ROOT)/tools/check_prach.py $(ROOT)/$(OBJ)/prach_dump.txt \
		--preamble $(if $(PRE),$(PRE),2) --ta $(if $(TA),$(TA),9) \
		--snr $(if $(SNR),$(SNR),6) --seed $(if $(SEED),$(SEED),1)

# ---- 4-step random access: Msg1 PRACH + Msg2 RAR + Msg3 PUSCH + Msg4 --------
# One SoC run does the whole process: the CGRA finds the preamble and the TA, the
# RA-RNTI is built from the occasion, Msg2's DCI carries the rv that places k0,
# the RAR hands its TC-RNTI to Msg3's descrambling and to Msg4's CRC mask, and
# Msg3's FFT window is the Msg1-measured TA.  OCC= must match the occasion the
# stimulus used (it picks both the RA-RNTI and the binary); MSG4_RV=2 gives a
# non-zero k0, so the DCI -> rv -> k0 coupling cannot be satisfied by a constant.
RACH_TD = $(ROOT)/build/rach/rach.td

PCGRA_TD = $(ROOT)/build/rach/prach_cgra.td

# 同一套 PRACH 检测，但相关微码由 L1/L2 编译器生成（16 PE x 8 tap 部分和，CPU 归约）
run-prach-cgra: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc
	@mkdir -p $(dir $(PCGRA_TD))
	python3 $(ROOT)/tools/nr_rach.py dump --prach-out $(PCGRA_TD) \
		--preamble $(if $(PRE),$(PRE),2) --ta $(if $(TA),$(TA),9) \
		--prach-snr $(if $(SNR),$(SNR),6) --seed $(if $(SEED),$(SEED),1)
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/prach_cgra_bm.bin@0x80000000 \
		--load $(PCGRA_TD)@0x80400000 \
		--results 0x80070000 --nwords 296 \
		--dump $(ROOT)/$(OBJ)/prach_cgra_dump.txt \
		--max-cycles 100000000
	python3 $(ROOT)/tools/check_prach_cgra.py $(ROOT)/$(OBJ)/prach_cgra_dump.txt \
		--preamble $(if $(PRE),$(PRE),2) --ta $(if $(TA),$(TA),9) \
		--snr $(if $(SNR),$(SNR),6) --seed $(if $(SEED),$(SEED),1)

run-rach: all
	env -u LD_LIBRARY_PATH $(MAKE) -C $(ROOT)/sw/soc rach RACH_OCC=$(if $(OCC),$(OCC),1)
	@mkdir -p $(dir $(RACH_TD))
	python3 $(ROOT)/tools/nr_rach.py dump --prach-out $(RACH_TD).prach --msg-out $(RACH_TD) \
		--preamble $(if $(PRE),$(PRE),2) --ta $(if $(TA),$(TA),9) \
		--occ $(if $(OCC),$(OCC),1) --seed $(if $(SEED),$(SEED),1) \
		$(if $(MSG4_RV),--msg4-rv $(MSG4_RV))
	@test -f $(ROOT)/build/fft/twiddle.bin || python3 $(ROOT)/tools/fft_plan.py lut $(ROOT)/build/fft/twiddle.bin
	env -u LD_LIBRARY_PATH $(BIN) \
		--load $(ROOT)/build/sw/rach_bm_$(if $(OCC),$(OCC),1).bin@0x80000000 \
		--load $(RACH_TD)@0x80400000 \
		--load $(RACH_TD).prach@0x80480000 \
		--load $(ROOT)/build/fft/twiddle.bin@0x80100000 \
		--results 0x80180000 --nwords 3620 \
		--dump $(ROOT)/$(OBJ)/rach_dump.txt \
		--max-cycles $(if $(MAXC),$(MAXC),100000000)
	python3 $(ROOT)/tools/check_rach.py $(ROOT)/$(OBJ)/rach_dump.txt \
		--preamble $(if $(PRE),$(PRE),2) --ta $(if $(TA),$(TA),9) \
		--occ $(if $(OCC),$(OCC),1) --seed $(if $(SEED),$(SEED),1) \
		$(if $(MSG4_RV),--msg4-rv $(MSG4_RV))
