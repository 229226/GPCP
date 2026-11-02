# Clock constraint for the CGRA synthesis/STA runs (iEDA iSTA reads this).
# The port is named `clk` in rtl/cgra/cgra_top.sv.
set clk_port_name clk
set CLK_FREQ_MHZ 500
if {[info exists env(CLK_FREQ_MHZ)]} {
  set CLK_FREQ_MHZ $::env(CLK_FREQ_MHZ)
} else {
  puts "Warning: CLK_FREQ_MHZ is not set, using $CLK_FREQ_MHZ MHz"
}

set clk_port [get_ports $clk_port_name]
create_clock -name core_clock -period [expr 1000.0 / $CLK_FREQ_MHZ] $clk_port
