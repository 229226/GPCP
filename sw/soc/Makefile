# Bare-metal programs for the GPCP SoC (CVA6, RV64GC)

CROSS   := riscv64-unknown-elf-
CC      := $(CROSS)gcc
OBJCOPY := $(CROSS)objcopy
OBJDUMP := $(CROSS)objdump

ARCH    := -march=rv64gc -mabi=lp64d -mcmodel=medany
CFLAGS  := $(ARCH) -O2 -ffreestanding -fno-builtin -nostdlib -nostartfiles -Wall -Wextra
LDFLAGS := $(ARCH) -nostdlib -nostartfiles -Wl,-T,link.ld -Wl,--build-id=none

BUILD := ../../build/sw

PROGS := cgra_bm dma_bm irq_bm ring_bm rxchain_bm fft_bm slot_rtos sync_bm sss_bm polar_sc_bm ssb_bm ssb_fft_bm ssb_slot_bm fftprobe_bm ssb_fid_bm pdsch_bm pdsch_bg2_bm pdsch_bn1_bm pdsch_gather_bm pdsch_iter_bm pdsch_full_bm pdsch_harq_bm pdcch_bm link_e2e_bm pusch_bm sib1_bm pucch_bm prach_bm pdsch_16qam_bm pdsch_64qam_bm pusch_tp_bm csi_bm mimo_ce_bm mimo_bm prach_cgra_bm pdsch_zc64_bm pdsch_bg1_bm pbch_tx_bm pbch_pack_bm ifft_bm

all: $(PROGS:%=$(BUILD)/%.bin)

# 共享运行时头：任何改动都必须触发全部基准重编，否则会悄悄跑旧二进制
COMMON_HDRS := ../include/soc_io.h ../include/gpcp_cgra.h \
               ../include/gpcp_layout.h $(wildcard *_layout.h)
$(PROGS:%=$(BUILD)/%.elf): $(COMMON_HDRS)

$(BUILD):
	mkdir -p $@

$(BUILD)/%.elf: %.c start.S link.ld | $(BUILD)
	env -u LD_LIBRARY_PATH $(CC) $(CFLAGS) $(LDFLAGS) start.S $< -o $@
	env -u LD_LIBRARY_PATH $(OBJDUMP) -d $@ > $(BUILD)/$*.dis

# BN/CN 微码由规则映射器生成（对应 .c 只 include 它）；改了映射器就重新生成
pdsch_bn_prog.h: ../../cgra/tools/gen_bn_prog.py ../../cgra/tools/rule_map.py
	python3 ../../cgra/tools/gen_bn_prog.py

pdsch_cn_prog.h: ../../cgra/tools/gen_cn_prog.py ../../cgra/tools/rule_map.py
	python3 ../../cgra/tools/gen_cn_prog.py

pdsch_cn20_prog.h: ../../cgra/tools/gen_kernel_prog.py ../../cgra/tools/rule_map.py ../../cgra/kernels/cn20.yaml
	python3 ../../cgra/tools/gen_kernel_prog.py --spec ../../cgra/kernels/cn20.yaml

$(BUILD)/pdsch_bn1_bm.elf: pdsch_bn_prog.h
$(BUILD)/pdsch_bg2_bm.elf: pdsch_cn_prog.h
$(BUILD)/pdsch_zc64_bm.elf: pdsch_cn_prog.h ../include/ldpc_bg2_ils0.h
$(BUILD)/pbch_tx_bm.elf: polar_inword_s0_prog.h polar_inword_s1_prog.h polar_inword_s2_prog.h polar_inword_s3_prog.h polar_inword_s4_prog.h polar_word_d1_prog.h polar_word_d2_prog.h polar_word_d4_prog.h polar_word_d8_prog.h
$(BUILD)/pdsch_bg1_bm.elf: pdsch_cn20_prog.h ../include/ldpc_bg1.h
	env -u LD_LIBRARY_PATH $(CC) $(CFLAGS) -DUSE_BG1=1 $(LDFLAGS) start.S pdsch_zc64_bm.c -o $@
	env -u LD_LIBRARY_PATH $(OBJDUMP) -d $@ > $(BUILD)/pdsch_bg1_bm.dis

# PRACH 相关微码由编译器生成（cgra/kernels/prach_corr.yaml）
$(BUILD)/prach_bm.elf: prach_corr_prog.h

# 编译器生成的 PRACH 相关微码（cgra/kernels/prach_corr.yaml）
prach_corr_prog.h: ../../cgra/tools/gen_kernel_prog.py ../../cgra/tools/rule_map.py ../../cgra/tools/kernel_spec.py ../../cgra/tools/bank_alloc.py ../../cgra/kernels/prach_corr.yaml
	python3 ../../cgra/tools/gen_kernel_prog.py --spec ../../cgra/kernels/prach_corr.yaml

sss_corr_prog.h: ../../cgra/tools/gen_kernel_prog.py ../../cgra/tools/rule_map.py ../../cgra/tools/kernel_spec.py ../../cgra/tools/bank_alloc.py ../../cgra/kernels/sss_corr.yaml
	python3 ../../cgra/tools/gen_kernel_prog.py --spec ../../cgra/kernels/sss_corr.yaml

polar_node_prog.h: ../../cgra/tools/gen_kernel_prog.py ../../cgra/tools/rule_map.py ../../cgra/tools/kernel_spec.py ../../cgra/tools/bank_alloc.py ../../cgra/kernels/polar_node.yaml
	python3 ../../cgra/tools/gen_kernel_prog.py --spec ../../cgra/kernels/polar_node.yaml

$(BUILD)/sss_bm.elf: sss_corr_prog.h
$(BUILD)/polar_sc_bm.elf: polar_node_prog.h

$(BUILD)/prach_cgra_bm.elf: prach_corr_prog.h

# run-rach: the whole 4-step process in one program, so it needs every kernel
# (PRACH correlation + Polar SC + LDPC CN/BN) plus the PDCCH LUTs.
# RACH_OCC = the PRACH occasion Msg1 was sent on, which is what the RA-RNTI is
# built from.  The binary is named after it, so an OCC=1 build can never be
# silently reused for an OCC=3 stimulus (AGENTS §5.4: "结果一字不变 → 先查构建").
RACH_OCC ?= 1
rach: $(BUILD)/rach_bm_$(RACH_OCC).bin

$(BUILD)/rach_bm_$(RACH_OCC).elf: rach_bm.c start.S link.ld pdcch_luts.h \
	                         pdsch_cn_prog.h pdsch_bn_prog.h prach_corr_prog.h | $(BUILD)
	env -u LD_LIBRARY_PATH $(CC) $(CFLAGS) -DOCC=$(RACH_OCC) $(LDFLAGS) \
		start.S rach_bm.c -o $@
	env -u LD_LIBRARY_PATH $(OBJDUMP) -d $@ > $(BUILD)/rach_bm_$(RACH_OCC).dis

.SECONDARY: $(BUILD)/rach_bm_$(RACH_OCC).elf

# interrupt test: needs the trap-vector startup instead of the plain one
$(BUILD)/irq_bm.elf: irq_bm.c start_irq.S link.ld | $(BUILD)
	env -u LD_LIBRARY_PATH $(CC) $(CFLAGS) $(LDFLAGS) start_irq.S $< -o $@
	env -u LD_LIBRARY_PATH $(OBJDUMP) -d $@ > $(BUILD)/irq_bm.dis

$(BUILD)/ring_bm.elf: ring_bm.c start_irq.S link.ld | $(BUILD)
	env -u LD_LIBRARY_PATH $(CC) $(CFLAGS) $(LDFLAGS) start_irq.S $< -o $@
	env -u LD_LIBRARY_PATH $(OBJDUMP) -d $@ > $(BUILD)/ring_bm.dis

$(BUILD)/rxchain_bm.elf: rxchain_bm.c start_irq.S link.ld | $(BUILD)
	env -u LD_LIBRARY_PATH $(CC) $(CFLAGS) $(LDFLAGS) start_irq.S $< -o $@
	env -u LD_LIBRARY_PATH $(OBJDUMP) -d $@ > $(BUILD)/rxchain_bm.dis

$(BUILD)/%.bin: $(BUILD)/%.elf
	env -u LD_LIBRARY_PATH $(OBJCOPY) -O binary $< $@
	@ls -l $@

# keep the ELF listings around for disassembly-driven debugging
.SECONDARY: $(PROGS:%=$(BUILD)/%.elf)

clean:
	rm -rf $(BUILD)

.PHONY: all clean rach

# slot scheduler: needs the trap vector (timer + PLIC) startup
$(BUILD)/slot_rtos.elf: slot_rtos.c start_irq.S link.ld | $(BUILD)
	env -u LD_LIBRARY_PATH $(CC) $(CFLAGS) $(LDFLAGS) start_irq.S $< -o $@
	env -u LD_LIBRARY_PATH $(OBJDUMP) -d $@ > $(BUILD)/slot_rtos.dis

# slot-scheduled SSB: P0.2 sample-level chain driven by the CLINT slot timer
$(BUILD)/ssb_slot_bm.elf: ssb_slot_bm.c start_irq.S link.ld | $(BUILD)
	env -u LD_LIBRARY_PATH $(CC) $(CFLAGS) $(LDFLAGS) start_irq.S $< -o $@
	env -u LD_LIBRARY_PATH $(OBJDUMP) -d $@ > $(BUILD)/ssb_slot_bm.dis
# P1 real-mapping SSB benchmark: needs the inverse rate-match LUT header
$(BUILD)/ssb_fid_bm.elf: ssb_fid_bm.c start.S link.ld | $(BUILD)
	@mkdir -p $(BUILD)
	env -u LD_LIBRARY_PATH $(CC) $(CFLAGS) -I../../tools $(LDFLAGS) start.S $< -o $@
	env -u LD_LIBRARY_PATH $(OBJDUMP) -d $@ > $(BUILD)/ssb_fid_bm.dis

# PDSCH full receive chain: needs the generated CN/BN microcode headers
$(BUILD)/pdsch_full_bm.elf: pdsch_cn_prog.h pdsch_bn_prog.h
$(BUILD)/pusch_bm.elf: pdsch_cn_prog.h pdsch_bn_prog.h

# DFT-s-OFDM variant: 360-point IDFT deprecoding, integer Q20 table.
$(BUILD)/pusch_tp_bm.elf: pusch_bm.c start.S link.ld pusch_idft_tab.h pdsch_cn_prog.h pdsch_bn_prog.h | $(BUILD)
	env -u LD_LIBRARY_PATH $(CC) $(CFLAGS) -DTP $(LDFLAGS) start.S pusch_bm.c -o $@
	env -u LD_LIBRARY_PATH $(OBJDUMP) -d $@ > $(BUILD)/pusch_tp_bm.dis

# CSI-RS + CQI report (no LDPC, no DMA; FFT + 80-point LS estimate)
$(BUILD)/csi_bm.elf: csi_bm.c start.S link.ld csi_cqi_tab.h | $(BUILD)
	env -u LD_LIBRARY_PATH $(CC) $(CFLAGS) $(LDFLAGS) start.S csi_bm.c -o $@
	env -u LD_LIBRARY_PATH $(OBJDUMP) -d $@ > $(BUILD)/csi_bm.dis

# 2x2 MIMO channel estimation (two antennas, FDM pilots)
$(BUILD)/mimo_ce_bm.elf: mimo_ce_bm.c start.S link.ld | $(BUILD)
	env -u LD_LIBRARY_PATH $(CC) $(CFLAGS) $(LDFLAGS) start.S mimo_ce_bm.c -o $@
	env -u LD_LIBRARY_PATH $(OBJDUMP) -d $@ > $(BUILD)/mimo_ce_bm.dis

# 2x2 MMSE receiver (channel estimate + per-RE solve + hard bits)
$(BUILD)/mimo_bm.elf: mimo_bm.c start.S link.ld | $(BUILD)
	env -u LD_LIBRARY_PATH $(CC) $(CFLAGS) $(LDFLAGS) start.S mimo_bm.c -o $@
	env -u LD_LIBRARY_PATH $(OBJDUMP) -d $@ > $(BUILD)/mimo_bm.dis

# HARQ: same full-chain source, compiled for two RV transmissions
$(BUILD)/pdsch_harq_bm.elf: pdsch_full_bm.c start.S link.ld pdsch_cn_prog.h pdsch_bn_prog.h | $(BUILD)
	env -u LD_LIBRARY_PATH $(CC) $(CFLAGS) -DHARQ_TX=2 $(LDFLAGS) start.S pdsch_full_bm.c -o $@
	env -u LD_LIBRARY_PATH $(OBJDUMP) -d $@ > $(BUILD)/pdsch_harq_bm.dis

# P4 MCS-driven downlink: same full-chain source with 16QAM max-log demapping.
# Its own RESULT_BASE (0x80070000) is clear of .bss -- keep the rule complete.
$(BUILD)/pdsch_16qam_bm.elf: pdsch_full_bm.c start.S link.ld pdsch_cn_prog.h pdsch_bn_prog.h | $(BUILD)
	env -u LD_LIBRARY_PATH $(CC) $(CFLAGS) -DQAM16 $(LDFLAGS) start.S pdsch_full_bm.c -o $@
	env -u LD_LIBRARY_PATH $(OBJDUMP) -d $@ > $(BUILD)/pdsch_16qam_bm.dis

# 64QAM variant of the same source; its RESULT_BASE is also 0x80070000.
$(BUILD)/pdsch_64qam_bm.elf: pdsch_full_bm.c start.S link.ld pdsch_cn_prog.h pdsch_bn_prog.h | $(BUILD)
	env -u LD_LIBRARY_PATH $(CC) $(CFLAGS) -DQAM64 $(LDFLAGS) start.S pdsch_full_bm.c -o $@
	env -u LD_LIBRARY_PATH $(OBJDUMP) -d $@ > $(BUILD)/pdsch_64qam_bm.dis

# PDCCH: inverse rate-matching LUTs are generated from the Python model
pdcch_luts.h: ../../tools/gen_pdcch_luts.py ../../tools/gen_luts.py ../../tools/nr_pbch.py
	python3 ../../tools/gen_pdcch_luts.py pdcch_luts.h

$(BUILD)/pdcch_bm.elf: pdcch_luts.h

# link-e2e: PDCCH (polar) + PDSCH (LDPC) fused in one run -- needs both LUT sets
$(BUILD)/link_e2e_bm.elf: pdcch_luts.h pdsch_cn_prog.h pdsch_bn_prog.h
# SIB1: same verified PDCCH+PDSCH source, compiled for the SI-RNTI case
$(BUILD)/sib1_bm.elf: link_e2e_bm.c start.S link.ld pdcch_luts.h pdsch_cn_prog.h pdsch_bn_prog.h | $(BUILD)
	env -u LD_LIBRARY_PATH $(CC) $(CFLAGS) -DSIB1 $(LDFLAGS) start.S link_e2e_bm.c -o $@
# PUCCH format 0: reuses the PRACH correlation microcode header
$(BUILD)/pucch_bm.elf: pucch_bm.c start.S link.ld prach_corr_prog.h | $(BUILD)
	env -u LD_LIBRARY_PATH $(CC) $(CFLAGS) $(LDFLAGS) start.S pucch_bm.c -o $@

# 真实 kernel tile：由 L1/L2 编译器生成（mmse_2x1 供 run-mimo 的 b = H^H y）
mmse_2x1_prog.h: ../../cgra/tools/gen_kernel_prog.py ../../cgra/tools/rule_map.py ../../cgra/tools/bank_alloc.py ../../cgra/kernels/mmse_2x1.yaml
	python3 ../../cgra/tools/gen_kernel_prog.py --spec ../../cgra/kernels/mmse_2x1.yaml

fft_butterfly_prog.h: ../../cgra/tools/gen_kernel_prog.py ../../cgra/tools/rule_map.py ../../cgra/tools/bank_alloc.py ../../cgra/kernels/fft_butterfly.yaml
	python3 ../../cgra/tools/gen_kernel_prog.py --spec ../../cgra/kernels/fft_butterfly.yaml

$(BUILD)/mimo_bm.elf: mmse_2x1_prog.h
