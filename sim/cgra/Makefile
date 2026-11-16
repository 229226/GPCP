# Verilator build of the M0 CGRA prototype
#
#   make            build ./build/Vcgra_axi_lite
#   make clean      remove build products

# Prefer the Verilator that ships with this project (third_party/eda, fetched by
# tools/fetch_eda.sh); fall back to PATH if it is not installed.
LOCAL_VERILATOR := $(wildcard $(CURDIR)/../../third_party/eda/oss-cad-suite/bin/verilator)
VERILATOR ?= $(if $(LOCAL_VERILATOR),$(LOCAL_VERILATOR),verilator)
TOP       := cgra_axi_lite

RTL_DIR := ../../rtl/cgra
OBJ_DIR := build
BIN     := $(OBJ_DIR)/V$(TOP)

RTL_SRCS := \
	$(RTL_DIR)/cgra_pkg.sv \
	$(RTL_DIR)/cgra_desc_pkg.sv \
	$(RTL_DIR)/cgra_pe.sv \
	$(RTL_DIR)/cgra_array.sv \
	$(RTL_DIR)/cgra_top.sv \
	$(RTL_DIR)/cgra_axi_lite.sv

VFLAGS := \
	--cc --exe --build -O3 -j 8 \
	--top-module $(TOP) \
	-I$(RTL_DIR) \
	-Wno-fatal -Wno-WIDTH -Wno-WIDTHTRUNC -Wno-WIDTHEXPAND \
	-Wno-UNOPTFLAT -Wno-DECLFILENAME -Wno-UNUSEDSIGNAL -Wno-UNUSEDPARAM

.PHONY: all clean rot-test

all: $(BIN)

rot-test: $(BIN)
	python3 rot_test.py

$(BIN): tb_cgra.cpp $(RTL_SRCS)
	@mkdir -p $(OBJ_DIR)
	+env -u LD_LIBRARY_PATH $(VERILATOR) $(VFLAGS) --Mdir $(OBJ_DIR) -o V$(TOP) $(RTL_SRCS) tb_cgra.cpp

clean:
	rm -rf $(OBJ_DIR)
