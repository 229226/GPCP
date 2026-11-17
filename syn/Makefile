# PPA flow for GPCP: Yosys (+slang) tech-maps the RTL onto an open standard-cell
# library, then iEDA's iSTA reports timing/power.
#
# Two PDKs are wired in, selected with PDK_NAME:
#   nangate45 (default)  45 nm, NangateOpenCellLibrary      -> build/syn/<design>-<freq>MHz
#   sky130hd            130 nm, sky130_fd_sc_hd (SkyWater)  -> build/syn/<design>-sky130-<freq>MHz
#
# The toolchain and the PDKs belong to this project (third_party/, gitignored
# and fetched by `make tools`); nothing here reads any other checkout on the
# machine.  iEDA is the open-source OSCC-Project/iEDA binary, the PDKs are
# OpenROAD-flow-scripts' flow/platforms/{nangate45,sky130hd} (Apache-2.0).
#
#   make tools                     # one-off: fetch yosys + iEDA + nangate45
#   make tools-sky130              # one-off: fetch the sky130 HD liberty
#   make -C syn sta CLK_FREQ_MHZ=500                    # CGRA on nangate45
#   make -C syn sta CLK_FREQ_MHZ=500 DESIGN=cpu_top      # CPU  on nangate45
#   make -C syn sta PDK_NAME=sky130hd CLK_FREQ_MHZ=50   # CGRA on sky130
#
# Only the liberty is needed: the flow is pre-layout (no LEF/SPEF), so area,
# timing and power all come from the library alone.

ROOT      := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)
# iEDA 的 STA/功耗支持多线程；Yosys/ABC 本身是单线程，JOBS 只影响 iEDA。
JOBS      ?= $(shell nproc)
EDA_HOME  ?= $(ROOT)/third_party/eda
IEDA      ?= $(EDA_HOME)/bin/iEDA
# Yosys + the slang frontend plugin ship in the project's own oss-cad-suite;
# fall back to PATH only if that has not been fetched yet.
LOCAL_YOSYS := $(wildcard $(EDA_HOME)/oss-cad-suite/bin/yosys)
YOSYS     ?= $(if $(LOCAL_YOSYS),$(LOCAL_YOSYS),yosys)
DESIGN    ?= cgra_top

PDK_NAME  ?= nangate45
ifeq ($(PDK_NAME),sky130hd)
PDK          ?= $(ROOT)/third_party/pdk/sky130hd
LIB          ?= $(PDK)/lib/sky130_fd_sc_hd__tt_025C_1v80.lib
MACRO_LIB    ?=
HI_CELL      ?= sky130_fd_sc_hd__conb_1
LO_CELL      ?= sky130_fd_sc_hd__conb_1
HI_PIN       ?= HI
LO_PIN       ?= LO
CLK_FREQ_MHZ ?= 50
OUT          := $(ROOT)/build/syn/$(DESIGN)-sky130-$(CLK_FREQ_MHZ)MHz
else ifeq ($(PDK_NAME),nangate45)
PDK          ?= $(ROOT)/third_party/pdk/nangate45
LIB          ?= $(PDK)/lib/NangateOpenCellLibrary_typical.lib
# 512x64 fakeRAM45: 8 lanes/bank x 2 banks = 16 macros for the 64 KiB IMEM.
MACRO_LIB    ?= $(PDK)/lib/fakeram45_512x64.lib
HI_CELL      ?= LOGIC1_X1
LO_CELL      ?= LOGIC0_X1
HI_PIN       ?= Z
LO_PIN       ?= Z
CLK_FREQ_MHZ ?= 500
OUT          := $(ROOT)/build/syn/$(DESIGN)-$(CLK_FREQ_MHZ)MHz
else
$(error PDK_NAME must be nangate45 or sky130hd, got '$(PDK_NAME)')
endif
NETLIST   := $(OUT)/$(DESIGN).netlist.syn.v
# IMEM 宏化是 opt-in：默认仍按“逻辑-only”跑（存储单列），
# `make -C syn syn IMEM_MACRO=1` 才把 IMEM 换成 fakeram45_512x64 宏。
IMEM_MACRO ?= 0
MACRO_SEL  := $(if $(filter 1,$(IMEM_MACRO)),$(if $(wildcard $(MACRO_LIB)),$(MACRO_LIB),),)
MACRO_ARG  := $(MACRO_SEL)
ABC_D     := $(shell echo $$((1000 / $(CLK_FREQ_MHZ))))
export LIB OUT CLK_FREQ_MHZ ABC_D HI_CELL LO_CELL HI_PIN LO_PIN PDK_NAME
# 脚本只看 MACRO_LIB；未开启宏化时为空 -> 走原行为路径。
export MACRO_LIB := $(MACRO_SEL)

.PHONY: all tools tools-sky130 syn sta area clean show cpu
all: sta

# CPU (CVA6/ariane) PPA: synthesize the core logic fabric.  OUT, NETLIST, the
# synthesis script and the sdc are all derived from DESIGN at parse time, so
# DESIGN must be set on the command line: re-invoke make instead of using a
# target-specific variable.  NOTE: synthesis (area) completes, but the bundled
# iEDA cannot link this 321k-cell netlist for STA - see docs/performance/README.md section 9.4.
cpu:
	$(MAKE) sta DESIGN=cpu_top PDK_NAME=$(PDK_NAME) CLK_FREQ_MHZ=$(CLK_FREQ_MHZ)

tools:
	bash $(ROOT)/tools/fetch_eda.sh

# sky130 HD liberty (13 MB) from OpenROAD-flow-scripts, same project-local rule
tools-sky130:
	PDK_NAME=sky130hd bash $(ROOT)/tools/fetch_eda.sh

syn: $(LIB) $(MACRO_ARG)
	mkdir -p $(OUT)
	env -u LD_LIBRARY_PATH $(YOSYS) -m slang -c $(ROOT)/syn/$(DESIGN)_syn.tcl -l $(OUT)/yosys.log

sta: syn
	env -u LD_LIBRARY_PATH CLK_FREQ_MHZ=$(CLK_FREQ_MHZ) OMP_NUM_THREADS=$(JOBS) \
	  $(IEDA) -thread_number $(JOBS) -script $(ROOT)/syn/sta.tcl \
	  $(ROOT)/syn/$(DESIGN).sdc $(NETLIST) $(DESIGN) $(LIB) $(MACRO_ARG) 2>&1 | tee $(OUT)/sta.log

# fail loudly instead of producing numbers from a different library
$(LIB):
	@echo "missing $(LIB)" >&2
	@echo "run 'make -C syn tools' (nangate45) or 'make -C syn tools-sky130' (sky130hd)" >&2
	@exit 1

# Area distribution of the PPA netlist: registers keep their RTL names, so they
# can be attributed to a functional block; ABC-named combinational cells cannot
# (the slang frontend flattens at read time, see syn/area_report.py).
area: $(NETLIST) $(LIB)
	python3 $(ROOT)/syn/area_report.py $(NETLIST) $(LIB) | tee $(OUT)/area_groups.txt

$(NETLIST):
	$(MAKE) syn DESIGN=$(DESIGN) PDK_NAME=$(PDK_NAME) CLK_FREQ_MHZ=$(CLK_FREQ_MHZ)

show:
	@echo "area:  $(OUT)/synth_stat.txt"
	@echo "timing: $(OUT)/$(DESIGN).rpt"
	@echo "breakdown: $(OUT)/area_groups.txt（make area 生成）"

clean:
	rm -rf $(ROOT)/build/syn
