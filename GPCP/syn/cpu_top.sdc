# Clock constraint for the CPU (cva6/ariane) synthesis/STA runs (iEDA iSTA).
# The cpu_top wrapper port is named `clk_i` (matches ariane's clk_i).
set clk_port_name clk_i
set CLK_FREQ_MHZ 500
if {[info exists env(CLK_FREQ_MHZ)]} {
  set CLK_FREQ_MHZ $::env(CLK_FREQ_MHZ)
} else {
  puts "Warning: CLK_FREQ_MHZ is not set, using $CLK_FREQ_MHZ MHz"
}

set clk_port [get_ports $clk_port_name]
create_clock -name core_clock -period [expr 1000.0 / $CLK_FREQ_MHZ] $clk_port
