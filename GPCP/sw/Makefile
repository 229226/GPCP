# Cross-compile check for the shared CGRA software ABI.
#
#   make        compile the bare-metal examples for RV32 (no link)
#   make rv64   same for RV64 + Linux-style ABI
#   make clean

SPECS  := /usr/lib/picolibc/riscv64-unknown-elf/picolibc.specs
CC     := riscv64-unknown-elf-gcc
BUILD  := ../build/sw

RV32_FLAGS := --specs=$(SPECS) -march=rv32im -mabi=ilp32
RV64_FLAGS := --specs=$(SPECS) -march=rv64gc -mabi=lp64d
CFLAGS     := -O2 -Wall -Wextra -Iinclude

SRCS := examples/cgra_matmul.c

.PHONY: all rv64 clean

all:
	@mkdir -p $(BUILD)
	@for s in $(SRCS); do \
		env -u LD_LIBRARY_PATH $(CC) $(RV32_FLAGS) $(CFLAGS) -c $$s -o $(BUILD)/$$(basename $$s .c).rv32.o || exit 1; \
	done
	@echo "RV32 compile check OK"

rv64:
	@mkdir -p $(BUILD)
	@for s in $(SRCS); do \
		env -u LD_LIBRARY_PATH $(CC) $(RV64_FLAGS) $(CFLAGS) -c $$s -o $(BUILD)/$$(basename $$s .c).rv64.o || exit 1; \
	done
	@echo "RV64 compile check OK"

clean:
	rm -rf $(BUILD)
