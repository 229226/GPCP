# PPA flow for GPCP: Yosys (+slang) tech-maps the RTL onto the Nangate45 open
# 45 nm standard-cell library, then iEDA's iSTA reports timing/power.
# The PDK and iEDA come from the local yosys-sta installation.

ROOT      := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)
STA_HOME  ?= $(HOME)/yosys-sta
PDK       ?= nangate45
LIB       := $(STA_HOME)/pdk/$(PDK)/lib/Nangate45_typ.lib
DESIGN    ?= cgra_top
CLK_FREQ_MHZ ?= 500
OUT       := $(ROOT)/build/syn/$(DESIGN)-$(CLK_FREQ_MHZ)MHz
NETLIST   := $(OUT)/$(DESIGN).netlist.syn.v
export LIB OUT CLK_FREQ_MHZ
export ABC_D := $(shell echo $$((1000 / $(CLK_FREQ_MHZ))))

.PHONY: all syn sta clean show
all: sta

syn:
	mkdir -p $(OUT)
	env -u LD_LIBRARY_PATH yosys -m slang -c $(ROOT)/syn/cgra_syn.tcl -l $(OUT)/yosys.log

sta: syn
	env -u LD_LIBRARY_PATH CLK_FREQ_MHZ=$(CLK_FREQ_MHZ) \
	  $(STA_HOME)/bin/iEDA -script $(STA_HOME)/scripts/sta.tcl \
	  $(ROOT)/syn/$(DESIGN).sdc $(NETLIST) $(DESIGN) $(PDK) 2>&1 | tee $(OUT)/sta.log

show:
	@echo "area:  $(OUT)/synth_stat.txt"
	@echo "timing: $(OUT)/$(DESIGN).rpt"

clean:
	rm -rf $(ROOT)/build/syn
