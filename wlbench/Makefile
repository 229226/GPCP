CC      ?= gcc
CFLAGS  ?= -O2 -std=c11 -Wall -Wextra -Wpedantic -ffp-contract=off
BUILD   := build
HOST    := $(BUILD)/wlbench_host
PHYTEST := $(BUILD)/phy_selftest
SRC     := src/wlbench.c src/wl_phy.c src/host_main.c
SRC_PHY := src/wl_phy.c src/phy_selftest.c
HDR     := include/wlbench.h include/wl_phy.h src/wl_math.h

DUMP    ?=
ARGS    ?= --scenario mixed --slots 200 --seed 1
CYCLES  ?=
SLOTS   ?= 200
CLOCK   ?= 500
CORRECT ?=

all: $(HOST)

$(BUILD):
	mkdir -p $(BUILD)

$(HOST): $(SRC) $(HDR) | $(BUILD)
	$(CC) $(CFLAGS) -Iinclude -o $@ $(SRC)

$(PHYTEST): $(SRC_PHY) $(HDR) | $(BUILD)
	$(CC) $(CFLAGS) -Iinclude -o $@ $(SRC_PHY)

# PHY 数值自检：FFT 对拍、星座功率、AWGN BER、链路 BLER 瀑布
phy: $(PHYTEST)
	$(PHYTEST)

test: $(HOST) $(PHYTEST)
	python3 testbench/check.py --host $(HOST) --selftest
	$(PHYTEST)

check: $(HOST)
	python3 testbench/check.py --host $(HOST) --dump $(DUMP) --args "$(ARGS)"

score:
	python3 eval/score.py --cycles $(CYCLES) --slots $(SLOTS) \
		--clock-mhz $(CLOCK) $(if $(CORRECT),--correct $(CORRECT),)

names: $(HOST)
	python3 testbench/check.py --host $(HOST) --print-names

smoke: $(HOST)
	$(HOST) --scenario mixed --slots 200 --seed 1

clean:
	rm -rf $(BUILD)

.PHONY: all test check score names smoke clean
