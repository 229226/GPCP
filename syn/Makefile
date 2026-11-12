# PPA flow for GPCP: Yosys (+slang) tech-maps the RTL onto the Nangate45 open
# 45 nm standard-cell library, then iEDA's iSTA reports timing/power.
#
# The toolchain and the PDK belong to this project (third_party/, gitignored and
# fetched by `make tools`); nothing here reads any other checkout on the
# machine.  iEDA is the open-source OSCC-Project/iEDA binary, the PDK is
# OpenROAD-flow-scripts' flow/platforms/nangate45 (Apache-2.0).
#
#   make tools                     # one-off: fetch yosys + iEDA + PDK (~2.3 GB)
#   make -C syn sta CLK_FREQ_MHZ=500   # CGRA (cgra_top)
#   make -C syn cpu CLK_FREQ_MHZ=500   # CPU  (cva6/ariane)

ROOT      := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)
EDA_HOME  ?= $(ROOT)/third_party/eda
IEDA      ?= $(EDA_HOME)/bin/iEDA
# Yosys + the slang frontend plugin ship in the project's own oss-cad-suite;
# fall back to PATH only if that has not been fetched yet.
LOCAL_YOSYS := $(wildcard $(EDA_HOME)/oss-cad-suite/bin/yosys)
YOSYS     ?= $(if $(LOCAL_YOSYS),$(LOCAL_YOSYS),yosys)
PDK       ?= $(ROOT)/third_party/pdk/nangate45
LIB       ?= $(PDK)/lib/NangateOpenCellLibrary_typical.lib
DESIGN    ?= cgra_top
CLK_FREQ_MHZ ?= 500
OUT       := $(ROOT)/build/syn/$(DESIGN)-$(CLK_FREQ_MHZ)MHz
NETLIST   := $(OUT)/$(DESIGN).netlist.syn.v
export LIB OUT CLK_FREQ_MHZ
export ABC_D := $(shell echo $$((1000 / $(CLK_FREQ_MHZ))))

.PHONY: all tools syn sta clean show cpu
all: sta

# CPU (CVA6/ariane) PPA: synthesize the core logic fabric.  OUT, NETLIST, the
# synthesis script and the sdc are all derived from DESIGN at parse time, so
# DESIGN must be set on the command line: re-invoke make instead of using a
# target-specific variable.  NOTE: synthesis (area) completes, but the bundled
# iEDA cannot link this 321k-cell netlist for STA - see docs/08 section 9.4.
cpu:
	$(MAKE) sta DESIGN=cpu_top CLK_FREQ_MHZ=$(CLK_FREQ_MHZ)

tools:
	bash $(ROOT)/tools/fetch_eda.sh

syn: $(LIB)
	mkdir -p $(OUT)
	env -u LD_LIBRARY_PATH $(YOSYS) -m slang -c $(ROOT)/syn/$(DESIGN)_syn.tcl -l $(OUT)/yosys.log

sta: syn
	env -u LD_LIBRARY_PATH CLK_FREQ_MHZ=$(CLK_FREQ_MHZ) \
	  $(IEDA) -script $(ROOT)/syn/sta.tcl \
	  $(ROOT)/syn/$(DESIGN).sdc $(NETLIST) $(DESIGN) $(LIB) 2>&1 | tee $(OUT)/sta.log

# fail loudly instead of producing numbers from a different library
$(LIB):
	@echo "missing $(LIB)" >&2
	@echo "run 'make -C syn tools' to fetch the project-local iEDA + Nangate45 PDK" >&2
	@exit 1

show:
	@echo "area:  $(OUT)/synth_stat.txt"
	@echo "timing: $(OUT)/$(DESIGN).rpt"

clean:
	rm -rf $(ROOT)/build/syn
