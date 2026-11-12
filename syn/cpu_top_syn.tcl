# Synthesis script for cpu_top (CVA6 / ariane) on Nangate45 (45 nm open cell
# library).  Run with:  yosys -m slang -c syn/cpu_syn.tcl
#
# Inputs come from the environment (see syn/Makefile):
#   LIB    - Nangate45 typical liberty file
#   OUT    - result directory
#   ABC_D  - ABC delay target in ps (1000 / CLK_FREQ_MHZ)
#
# The RTL is the full CVA6 (SystemVerilog with packages and wildcard imports),
# so the classic yosys frontend cannot parse it: read_slang (yosys-slang
# plugin) is used.  The CVA6 file list (sim/soc/cva6.f) is read and the
# testbench-only cva6_rvfi module is dropped (it is never instantiated by the
# core, only by the simulation testharness).
#
# Caches / TLBs / BTB / register files are inferred as $mem_v2 arrays.  This
# flow cannot map them to the PDK's fakeRAM45 macros (multi-port / banking would
# explode the netlist), and lowering to flops is impractical.  So, exactly like
# the CGRA flow, we record their size, drop them, and synthesize the *logic*
# fabric.  Storage is accounted for separately (docs/08).  This keeps the CPU
# and CGRA numbers methodologically identical: both are "logic fabric, RAM
# excluded" pre-layout fmax estimates.
#
# The RVFI probe default types are patched by scripts/patch_cva6.py so slang can
# elaborate cva6_rvfi_probes (its body accesses struct members, but its default
# parameter type is `logic`).  RVFI is debug-only observation logic.

set DESIGN cpu_top
set LIB    $::env(LIB)
set OUT    $::env(OUT)
set ABC_D  $::env(ABC_D)
set ROOT   [file normalize [file join [file dirname [info script]] ..]]

# absolute include paths: this script is invoked from syn/ (make -C syn), so
# repo-relative -I flags would not resolve
set INCDIRS [list \
  -I$ROOT/third_party/cva6/core/include \
  -I$ROOT/third_party/cva6/vendor/pulp-platform/common_cells/include \
  -I$ROOT/third_party/cva6/vendor/pulp-platform/axi/include \
  -I$ROOT/third_party/cva6/corev_apu/register_interface/include \
  -I$ROOT/third_party/cva6/core/cvfpu/src \
  -I$ROOT/third_party/cva6/corev_apu/tb ]

# read the CVA6 file list, dropping the testbench-only cva6_rvfi module
set fh [open [file join $ROOT sim/soc/cva6.f] r]
set raw [split [read $fh] \n]
close $fh
set srcs {}
foreach l $raw {
  set l [string trim $l]
  if {$l eq ""} { continue }
  # testbench-only module, never instantiated by the core
  if {[string match "*cva6_rvfi.sv" $l]} { continue }
  # duplicate package definition: riscv_pkg.sv already provides `package riscv`
  if {[string match "*pmp/include/riscv.sv" $l]} { continue }
  # duplicate module: openc910 and opene906 both define `gated_clk_cell`;
  # keep the openc910 one (identical cell) to satisfy both FPU vendors
  if {[string match "*opene906*/clk/rtl/gated_clk_cell.v" $l]} { continue }
  lappend srcs [file join $ROOT $l]
}
lappend srcs [file join $ROOT syn/cpu_top.sv]

yosys -import
read_slang --top $DESIGN {*}$INCDIRS {*}$srcs

yosys proc
opt
memory_dff
opt
memory_collect
tee -o $OUT/mem_stat.txt stat
# drop inferred RAM arrays and $print cells (debug keeps) - storage is
# accounted for separately, consistent with the CGRA flow
yosys delete {t:$mem_v2}
yosys delete {t:$print}
yosys select -clear
opt_clean -purge

# Same library, same 500 MHz target, same ABC recipe as the CGRA flow, but
# without the generic `synth` wrapper: on CVA6 the FPU + register file produce
# very wide mux cones (1.3 M wire bits), and synth's repeated coarse/fine opt
# loop does not converge in practical time.  flatten + techmap + dfflibmap +
# abc gives an equivalent gate-level netlist for STA.
flatten
opt_clean -purge
techmap
opt -purge
dfflibmap -liberty $LIB
opt -undriven
abc -D $ABC_D -liberty $LIB \
    -script "+strash;ifraig;retime,{D},-M,6;strash;dch,-f;map,-p,-M,1,{D},-f;topo;dnsize;buffer,-p,-N,16;upsize;"
hilomap -singleton -hicell LOGIC1_X1 Z -locell LOGIC0_X1 Z
setundef -zero
opt_clean -purge

read_liberty -lib $LIB
tee -o $OUT/synth_check.txt check -mapped
tee -o $OUT/synth_stat.txt stat -liberty $LIB
write_verilog -noattr -noexpr -nohex -nodec $OUT/$DESIGN.netlist.syn.v
