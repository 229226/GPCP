# Synthesis script for cgra_top on Nangate45 (45 nm open cell library).
# Run with:  yosys -c syn/cgra_syn.tcl -l build/syn/.../yosys.log
#
# Inputs come from the environment (see syn/Makefile):
#   LIB    - Nangate45 typical liberty file
#   OUT    - result directory
#   ABC_D  - ABC delay target in ps (1000 / CLK_FREQ_MHZ)
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

yosys -import
read_slang --top $DESIGN $RTL/cgra_pkg.sv $RTL/cgra_pe.sv \
           $RTL/cgra_array.sv $RTL/cgra_axi_lite.sv $RTL/cgra_top.sv

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
    -script "+strash;ifraig;retime,{D},-M,6;strash;dch,-f;map,-p,-M,1,{D},-f;topo;dnsize;buffer,-p;upsize;"
hilomap -singleton -hicell LOGIC1_X1 Z -locell LOGIC0_X1 Z
setundef -zero
opt_clean -purge

read_liberty -lib $LIB
tee -o $OUT/synth_check.txt check -mapped
tee -o $OUT/synth_stat.txt stat -liberty $LIB
write_verilog -noattr -noexpr -nohex -nodec $OUT/$DESIGN.netlist.syn.v
