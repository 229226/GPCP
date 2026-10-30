# Verilator model of the GPCP SoC (CVA6 + CGRA), milestone B2
#
#   make          build sim/soc/build/Vgpcp_soc (this takes a while: CVA6 is large)
#   make run      run the bare-metal CGRA test and compare results
#   make clean

ROOT      := ../..
OBJ       := sim/soc/build
BIN       := $(ROOT)/$(OBJ)/Vgpcp_soc
VERILATOR ?= verilator

INC := \
	-Ithird_party/cva6/core/include \
	-Ithird_party/cva6/vendor/pulp-platform/common_cells/include \
	-Ithird_party/cva6/vendor/pulp-platform/axi/include \
	-Ithird_party/cva6/corev_apu/register_interface/include \
	-Ithird_party/cva6/core/cvfpu/src \
	-Ithird_party/cva6/corev_apu/tb

SRCS := rtl/cgra/cgra_pkg.sv \
	rtl/cgra/cgra_pe.sv \
	rtl/cgra/cgra_array.sv \
	rtl/cgra/cgra_top.sv \
	rtl/soc/gpcp_soc.sv \
	sim/soc/tb_soc.cpp

VFLAGS := --cc --exe --build -O2 --build-jobs 2 --timing \
	--top-module gpcp_soc \
	-Wno-fatal -Wno-BLKANDNBLK -Wno-UNOPTFLAT -Wno-WIDTH -Wno-WIDTHTRUNC \
	-Wno-WIDTHEXPAND -Wno-CASEINCOMPLETE -Wno-LATCH -Wno-UNUSEDSIGNAL \
	-Wno-UNUSEDPARAM -Wno-DECLFILENAME -Wno-INITIALDLY

.PHONY: all run clean

all:
	mkdir -p $(ROOT)/$(OBJ)
	cd $(ROOT) && env -u LD_LIBRARY_PATH $(VERILATOR) $(VFLAGS) $(INC) \
		-f sim/soc/cva6.f $(SRCS) --Mdir $(OBJ) -o Vgpcp_soc

run: all
	$(MAKE) -C $(ROOT)/sw/soc
	env -u LD_LIBRARY_PATH $(BIN) --prog $(ROOT)/build/sw/cgra_bm.bin --results 0x400 --nwords 24 \
		--dump $(ROOT)/$(OBJ)/soc_dump.txt
	python3 $(ROOT)/tools/check_soc.py $(ROOT)/$(OBJ)/soc_dump.txt

clean:
	rm -rf $(ROOT)/$(OBJ)
