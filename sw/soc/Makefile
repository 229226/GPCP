# Bare-metal programs for the GPCP SoC (CVA6, RV64GC)

CROSS   := riscv64-unknown-elf-
CC      := $(CROSS)gcc
OBJCOPY := $(CROSS)objcopy
OBJDUMP := $(CROSS)objdump

ARCH    := -march=rv64gc -mabi=lp64d -mcmodel=medany
CFLAGS  := $(ARCH) -O2 -ffreestanding -fno-builtin -nostdlib -nostartfiles -Wall -Wextra
LDFLAGS := $(ARCH) -nostdlib -nostartfiles -Wl,-T,link.ld -Wl,--build-id=none

BUILD := ../../build/sw

PROGS := cgra_bm dma_bm irq_bm ring_bm rxchain_bm fft_bm slot_rtos sync_bm sss_bm polar_sc_bm ssb_bm ssb_fft_bm ssb_slot_bm fftprobe_bm ssb_fid_bm pdsch_bm pdsch_bg2_bm pdsch_bn1_bm pdsch_gather_bm

all: $(PROGS:%=$(BUILD)/%.bin)

$(BUILD):
	mkdir -p $@

$(BUILD)/%.elf: %.c start.S link.ld | $(BUILD)
	env -u LD_LIBRARY_PATH $(CC) $(CFLAGS) $(LDFLAGS) start.S $< -o $@
	env -u LD_LIBRARY_PATH $(OBJDUMP) -d $@ > $(BUILD)/$*.dis

# BN 微码由规则映射器生成（pdsch_bn1_bm.c 只 include 它）；改了映射器就重新生成
pdsch_bn_prog.h: ../../cgra/tools/gen_bn_prog.py ../../cgra/tools/rule_map.py
	python3 ../../cgra/tools/gen_bn_prog.py

$(BUILD)/pdsch_bn1_bm.elf: pdsch_bn_prog.h

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

.PHONY: all clean

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
