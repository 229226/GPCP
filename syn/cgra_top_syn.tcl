# Synthesis script for cgra_top.  Tech-maps onto whichever liberty the Makefile
# selects (Nangate45 by default, or sky130_fd_sc_hd with PDK_NAME=sky130hd).
# Run with:  yosys -c syn/cgra_top_syn.tcl -l build/syn/.../yosys.log
#
# Inputs come from the environment (see syn/Makefile):
#   LIB    - standard-cell liberty file
#   OUT    - result directory
#   ABC_D  - ABC delay target
#   HI_CELL/LO_CELL/HI_PIN/LO_PIN - constant cells of that library
#
# The RTL is SystemVerilog with a package and wildcard imports, which the
# classic yosys frontend cannot parse: read_slang (yosys-slang plugin) is used.
#
# Two storage arrays are inferred (32 KiB microcode IMEM and the 4 KiB banked
# scratchpad).  This flow cannot map them to the PDK's fakeRAM45 macros (a flat
# 8192x32 array with 16 read ports would need 16 replicated macros), and
# lowering them to flops makes the netlist explode (~300k FFs plus megabyte
# read muxes).  So: record their exact size, drop them, and synthesize the
# *logic* fabric.  Storage is added back from the fakeRAM macro LEFs in
# docs/08.

set DESIGN cgra_top
set LIB    $::env(LIB)
set OUT    $::env(OUT)
set ABC_D  $::env(ABC_D)
set RTL    [file join [file dirname [info script]] .. rtl cgra]
set MACRO_LIB [expr {[info exists ::env(MACRO_LIB)] ? $::env(MACRO_LIB) : ""}]

yosys -import
# SRAM macro liberty (fakeRAM45 / sky130 OpenRAM) is read as a blackbox library
# so the explicit IMEM macro instances survive synthesis and show up in the
# netlist.  Kept optional: without it the flow falls back to the behavioural
# IMEM and deletes all inferred memories as before.
if {$MACRO_LIB ne ""} {
  read_liberty -lib $MACRO_LIB
}
if {$MACRO_LIB ne ""} {
  # -DIMEM_MACRO selects the explicit fakeRAM45 512x64 lane instances in
  # cgra_top.sv (8 x 64-bit lanes per bank = 16 macros for the 64 KiB IMEM).
  read_slang --top $DESIGN -DSYNTHESIS -DIMEM_MACRO \
             $RTL/cgra_pkg.sv $RTL/cgra_desc_pkg.sv $RTL/cgra_pe.sv \
             $RTL/cgra_array.sv $RTL/cgra_axi_lite.sv $RTL/cgra_top.sv
} else {
  read_slang --top $DESIGN -DSYNTHESIS $RTL/cgra_pkg.sv $RTL/cgra_desc_pkg.sv \
             $RTL/cgra_pe.sv $RTL/cgra_array.sv $RTL/cgra_axi_lite.sv \
             $RTL/cgra_top.sv
}

yosys proc
opt
memory_dff
opt
memory_collect
tee -o $OUT/mem_stat.txt stat
# drop the inferred RAM arrays (and the $display cells, which set a `keep`
# attribute and block optimization): storage is accounted for separately
yosys delete {t:$mem_v2}
yosys delete {t:$print}
yosys select -clear
opt_clean -purge

# generic synthesis + technology mapping of the remaining logic
synth -top $DESIGN -flatten
autoname
renames -wire
splitnets -ports
opt -purge
dfflibmap -liberty $LIB
opt -undriven
abc -D $ABC_D -liberty $LIB \
    -script "+strash;ifraig;retime,{D},-M,6;strash;dch,-f;map,-p,-M,1,{D},-f;topo;dnsize;buffer,-p,-N,16;upsize;"
# constant nets -> the library's tie cells (Nangate: LOGIC1_X1/LOGIC0_X1 pin Z,
# sky130: sky130_fd_sc_hd__conb_1 with HI/LO outputs)
set HI_CELL [expr {[info exists ::env(HI_CELL)] ? $::env(HI_CELL) : "LOGIC1_X1"}]
set LO_CELL [expr {[info exists ::env(LO_CELL)] ? $::env(LO_CELL) : "LOGIC0_X1"}]
set HI_PIN  [expr {[info exists ::env(HI_PIN)]  ? $::env(HI_PIN)  : "Z"}]
set LO_PIN  [expr {[info exists ::env(LO_PIN)]  ? $::env(LO_PIN)  : "Z"}]
hilomap -singleton -hicell $HI_CELL $HI_PIN -locell $LO_CELL $LO_PIN
setundef -zero
opt_clean -purge

read_liberty -lib $LIB
tee -o $OUT/synth_check.txt check -mapped
tee -o $OUT/synth_stat.txt stat -liberty $LIB
write_verilog -noattr -noexpr -nohex -nodec $OUT/$DESIGN.netlist.syn.v
