# Verilator build of the M0 CGRA prototype
#
#   make            build ./build/Vcgra_axi_lite
#   make clean      remove build products

VERILATOR ?= verilator
TOP       := cgra_axi_lite

RTL_DIR := ../../rtl/cgra
OBJ_DIR := build
BIN     := $(OBJ_DIR)/V$(TOP)

RTL_SRCS := \
	$(RTL_DIR)/cgra_pkg.sv \
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

.PHONY: all clean

all: $(BIN)

$(BIN): tb_cgra.cpp $(RTL_SRCS)
	@mkdir -p $(OBJ_DIR)
	+env -u LD_LIBRARY_PATH $(VERILATOR) $(VFLAGS) --Mdir $(OBJ_DIR) -o V$(TOP) $(RTL_SRCS) tb_cgra.cpp

clean:
	rm -rf $(OBJ_DIR)
