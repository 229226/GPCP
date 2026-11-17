# -----------------------------------------------------------------------------
# Static timing + power for the synthesised CGRA netlist (iEDA / iSTA).
#
# This script is the project's own: it only uses the iEDA Tcl API that the
# PPA flow needs, and reads the project-local PDK (third_party/pdk/nangate45,
# fetched by tools/fetch_eda.sh).  No LEF/RC is loaded, so the result is a
# *pre-layout* estimate: no placement, no clock tree, no wire delay.
#
# argv: <sdc> <netlist.v> <design> <liberty>
# -----------------------------------------------------------------------------
set SDC_FILE   [lindex $argv 0]
set NETLIST_V  [lindex $argv 1]
set DESIGN     [lindex $argv 2]
set LIB_FILE   [lindex $argv 3]
set RESULT_DIR [file dirname $NETLIST_V]

set_design_workspace $RESULT_DIR

read_netlist $NETLIST_V
read_liberty $LIB_FILE
# optional SRAM macro liberty (fakeRAM45 / sky130 OpenRAM), passed as argv 5
if {[llength $argv] >= 5} {
  set MACRO_FILE [lindex $argv 4]
  if {$MACRO_FILE ne ""} { read_liberty $MACRO_FILE }
}
link_design $DESIGN
read_sdc  $SDC_FILE

# worst 5 setup paths (the report carries arrival time and slack per path)
report_timing -max_path 5
# switching activity is the tool default (0.1); no VCD is available here
report_power -toggle 0.1
