# Bare-metal programs for the GPCP SoC (CVA6, RV64GC)

CROSS   := riscv64-unknown-elf-
CC      := $(CROSS)gcc
OBJCOPY := $(CROSS)objcopy
OBJDUMP := $(CROSS)objdump

ARCH    := -march=rv64gc -mabi=lp64d -mcmodel=medany
CFLAGS  := $(ARCH) -O2 -ffreestanding -fno-builtin -nostdlib -nostartfiles -Wall -Wextra
LDFLAGS := $(ARCH) -nostdlib -nostartfiles -Wl,-T,link.ld -Wl,--build-id=none

BUILD := ../../build/sw

PROGS := cgra_bm dma_bm

all: $(PROGS:%=$(BUILD)/%.bin)

$(BUILD):
	mkdir -p $@

$(BUILD)/%.elf: %.c start.S link.ld | $(BUILD)
	env -u LD_LIBRARY_PATH $(CC) $(CFLAGS) $(LDFLAGS) start.S $< -o $@
	env -u LD_LIBRARY_PATH $(OBJDUMP) -d $@ > $(BUILD)/$*.dis

$(BUILD)/%.bin: $(BUILD)/%.elf
	env -u LD_LIBRARY_PATH $(OBJCOPY) -O binary $< $@
	@ls -l $@

# keep the ELF listings around for disassembly-driven debugging
.SECONDARY: $(PROGS:%=$(BUILD)/%.elf)

clean:
	rm -rf $(BUILD)

.PHONY: all clean
