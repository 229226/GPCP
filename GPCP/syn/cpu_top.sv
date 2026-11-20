`include "rvfi_types.svh"
// Standalone CPU (CVA6/ariane) synthesis top for the GPCP PPA flow.
// Mirrors rtl/soc/gpcp_soc.sv ariane instantiation: the config is connected
// explicitly (build_config(...)), not left at the empty default, so the
// parameterized cva6_cfg_t (and every derived type) resolves.
//
// Interface = the CPU's AXI master (request out, response in), exactly like
// the SoC.  Both ports must be real ports:
//   * yosys deletes every cone that does not reach a primary output, so
//     noc_req_o must be an output or the whole core collapses to 0 cells;
//   * noc_resp_i must be an input, not tied to '0, or constant propagation
//     sees load data == 0, marks the register file / data cache / ALU cone
//     dead and keeps only the request side (that produced a 1k-FF stub).
// rvfi_probes_o stays on an internal net: the SoC only feeds it to a $display
// ($print cell, dropped by the flow), so RVFI observation logic is optimized
// away here too.
module cpu_top (
  input  logic             clk_i,
  input  logic             rst_ni,
  input  ariane_axi::resp_t noc_resp_i,
  output ariane_axi::req_t  noc_req_o
);
  import ariane_pkg::*;
  import ariane_axi::*;

  localparam config_pkg::cva6_cfg_t CVA6Cfg =
      build_config_pkg::build_config(cva6_config_pkg::cva6_cfg);

  localparam type rvfi_instr_t = `RVFI_PROBES_INSTR_T(CVA6Cfg);
  typedef struct packed {
    logic        csr;
    rvfi_instr_t instr;
  } rvfi_probes_t;

  rvfi_probes_t rvfi_probes;
  logic [63:0]  mtime;
  logic         time_irq;

  assign mtime    = 64'h0;
  assign time_irq = 1'b0;

  ariane #(
      .CVA6Cfg             (CVA6Cfg),
      .rvfi_probes_instr_t (rvfi_instr_t),
      .rvfi_probes_t       (rvfi_probes_t)
  ) u_core (
      .clk_i         (clk_i),
      .rst_ni        (rst_ni),
      .boot_addr_i   (64'h8000_0000),
      .hart_id_i     (64'h0),
      .irq_i         ({1'b0, 1'b0}),
      .ipi_i         (1'b0),
      .time_irq_i    (time_irq),
      .mtime_i       (mtime),
      .debug_req_i   (1'b0),
      .rvfi_probes_o (rvfi_probes),
      .noc_req_o     (noc_req_o),
      .noc_resp_i    (noc_resp_i)
  );
endmodule
