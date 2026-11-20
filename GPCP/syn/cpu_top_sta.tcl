# Full-core CVA6 timing with OpenSTA.  The bundled iEDA cannot link this
# netlist (wide struct ports + alias assigns), so the PPA flow for the CPU is:
#
#   make -C syn cpu CLK_FREQ_MHZ=500                # synthesis (area is valid,
#                                                   # iEDA STA will fail)
#   python3 syn/split_concat_assign.py \
#       build/syn/cpu_top-500MHz/cpu_top.netlist.syn.v \
#       build/syn/cpu_top-500MHz/cpu_top.netlist.split.v   # bit-blast assigns
#   python3 syn/insert_buffers.py \
#       build/syn/cpu_top-500MHz/cpu_top.netlist.split.v \
#       build/syn/cpu_top-500MHz/cpu_top.netlist.buf.v 32  # buffer trees
#   sta -no_init -no_splash -exit syn/cpu_top_sta.tcl      # this file
#
# OpenSTA itself: `apt-get download opensta && dpkg -x opensta_*.deb dir` runs
# without root (version used: 2.0.17).
read_liberty /home/zzq/GPCP/third_party/pdk/nangate45/lib/NangateOpenCellLibrary_typical.lib
read_verilog /home/zzq/GPCP/build/syn/cpu_top-500MHz/cpu_top.netlist.buf.v
link_design cpu_top
read_sdc /home/zzq/GPCP/syn/cpu_top.sdc

report_wns
report_tns
report_checks -path_delay max -group_count 8 -format summary -digits 3
report_checks -path_delay max -group_count 1 -digits 3
