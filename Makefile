# GPCP top-level convenience targets

.PHONY: all test build clean verify verify-smart verify-full verify-slow verify-changed devcheck devreport handprog-check yaml-check snr-sweep snr-sweep-hw sw-check soc soc-run soc-run-dma soc-run-irq soc-run-ring soc-run-rxchain soc-run-fft soc-run-sync soc-run-sss soc-run-polar soc-run-ssb soc-run-ssbfft soc-run-fftprobe soc-run-slot soc-run-ssb-slot linux soc-run-prach soc-run-rach soc-run-pucch soc-run-pdsch-16qam soc-run-proto-nr_pdcch soc-run-proto-nr_pdsch soc-run-proto-nr_csi soc-run-proto-nr_prach soc-run-proto-nr_pucch soc-run-proto-nr_mimo soc-run-proto-nr_pusch soc-run-proto-nr_ssb soc-run-proto-lte_pdsch soc-run-proto-nr_pdsch_16qam soc-run-proto-nr_pdsch_64qam soc-run-proto-nr_pdsch_harq soc-run-proto-nr_pusch_tp soc-run-proto-lte_pusch soc-run-proto-lte_prach soc-run-proto-lte_cell_search soc-run-proto-wifi_11be kernels

all: test

# One-command acceptance: kernels + a quick SoC subset
# ---- 协议描述层（docs/20）----------------------------------------------------
# make proto-check : 静态检查（硬件无关 + 换厨房 + 落点 + 生成物），秒级
# make proto PROTO=<name> : 完整流水（生成 -> 编译 -> 运行 -> 评估 -> 报告）
proto-check:
	python3 proto/check_hll.py proto/scenarios/*.py
	python3 proto/compile.py --all
	python3 proto/emit.py --all

PROTO ?= nr_pdcch
proto:
	python3 proto/check_hll.py proto/scenarios/$(PROTO).py
	python3 proto/compile.py --scenario $(PROTO)
	python3 proto/emit.py --scenario $(PROTO)
	@ref=$$(python3 proto/proto_eval.py --scenario $(PROTO) --ref-gate); \
	if [ -n "$$ref" ]; then $(MAKE) -C sim/soc $$ref; fi
	$(MAKE) -C sim/soc run-proto-$(PROTO)
	@ref=$$(python3 proto/proto_eval.py --scenario $(PROTO) --ref-gate); \
	if [ -n "$$ref" ]; then \
	  python3 proto/proto_eval.py --scenario $(PROTO) \
	    --dump sim/soc/build/proto_$(PROTO)_dump.txt --ref auto; \
	fi

proto-all:
	python3 proto/check_hll.py proto/scenarios/*.py
	python3 proto/compile.py --all
	python3 proto/emit.py --all

.PHONY: proto-check proto proto-all

verify:
	bash tools/verify.sh quick

# RECOMMENDED for daily work: all 47 gates, only re-run changed inputs
verify-smart:
	bash tools/verify.sh smart

# Full SoC benchmark matrix (parallel -j6, ~10-12 min)
verify-full:
	bash tools/verify.sh full

# full + the long run-rach gate (11.8 M cycles, ~9 min)
verify-slow:
	bash tools/verify.sh slow

# Only the gates touched by the current git diff (~0.5-3 min)
verify-changed:
	bash tools/verify.sh changed

# 铁律：微码只许编译器生成（AGENTS.md 4.18）。每次 verify 也会自动跑。
handprog-check:
	python3 tools/check_handprog.py

# 新增 kernel 的独立黄金模型对账（ISA 仿真 + 算法规格公式，docs/22 待补项）
new-kernels-check:
	python3 cgra/tools/gen_kernel_prog.py --all
	python3 tools/check_new_kernels.py

# 铁律：kernel YAML 不直接描述硬件（AGENTS.md 4.19）。每次 verify 也会自动跑。
yaml-check:
	python3 tools/check_yaml_coupling.py

# 开发工时台账：校验（每个功能/修复提交都要有记录）与汇总
devcheck:
	python3 tools/devlog.py check

devreport:
	python3 tools/devlog.py report

# SNR waterfall of the sample-level SSB receiver (first quantitative PHY metric)
snr-sweep: all sw-check
	python3 tools/sweep_snr.py --model --snr -16 -12 -8 -4 0 4 8

snr-sweep-hw: all sw-check
	python3 tools/sweep_snr.py --hw --snr -8 -4 0 4 8

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

# PRACH Msg1: ZC cyclic-shift search on the CGRA -> preamble index + TA
soc-run-prach: ; $(MAKE) -C sim/soc run-prach
# 4-step random access: Msg1 -> Msg2 RAR -> Msg3 PUSCH -> Msg4, one SoC run
soc-run-rach: ; $(MAKE) -C sim/soc run-rach
# PUCCH format 0: 1-bit SR / HARQ-ACK on the CGRA
soc-run-pucch: ; $(MAKE) -C sim/soc run-pucch
# P4 MCS-driven downlink: 16QAM PDSCH
soc-run-pdsch-16qam: ; $(MAKE) -C sim/soc run-pdsch-16qam
# docs/20 阶段 2：协议运行器（场景 -> plan.h -> 运行 -> 评估）
soc-run-proto-nr_pdcch: ; $(MAKE) -C sim/soc run-proto-nr_pdcch
soc-run-proto-nr_pdsch: ; $(MAKE) -C sim/soc run-proto-nr_pdsch
soc-run-proto-nr_csi: ; $(MAKE) -C sim/soc run-proto-nr_csi
soc-run-proto-nr_prach: ; $(MAKE) -C sim/soc run-proto-nr_prach
soc-run-proto-nr_pucch: ; $(MAKE) -C sim/soc run-proto-nr_pucch
soc-run-proto-nr_mimo: ; $(MAKE) -C sim/soc run-proto-nr_mimo
soc-run-proto-nr_pusch: ; $(MAKE) -C sim/soc run-proto-nr_pusch
soc-run-proto-nr_ssb: ; $(MAKE) -C sim/soc run-proto-nr_ssb
soc-run-proto-lte_pdsch: ; $(MAKE) -C sim/soc run-proto-lte_pdsch
# 多制式扩展批次：5G 变体 4 + LTE 3 + WiFi 1
soc-run-proto-nr_pdsch_16qam: ; $(MAKE) -C sim/soc run-proto-nr_pdsch_16qam
soc-run-proto-nr_pdsch_64qam: ; $(MAKE) -C sim/soc run-proto-nr_pdsch_64qam
soc-run-proto-nr_pdsch_harq: ; $(MAKE) -C sim/soc run-proto-nr_pdsch_harq
soc-run-proto-nr_pusch_tp: ; $(MAKE) -C sim/soc run-proto-nr_pusch_tp
soc-run-proto-lte_pusch: ; $(MAKE) -C sim/soc run-proto-lte_pusch
soc-run-proto-lte_prach: ; $(MAKE) -C sim/soc run-proto-lte_prach
soc-run-proto-lte_cell_search: ; $(MAKE) -C sim/soc run-proto-lte_cell_search
soc-run-proto-wifi_11be: ; $(MAKE) -C sim/soc run-proto-wifi_11be

# B3: OpenSBI + Linux on the SoC
linux:
	$(MAKE) -C sim/soc linux

# L1 微码自动化：按 cgra/kernels/*.yaml 批量生成 sw/soc/*_prog.h
kernels:
	python3 cgra/tools/gen_kernel_prog.py --all

clean:
	rm -rf build sim/cgra/build sim/soc/build
