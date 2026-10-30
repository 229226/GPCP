// -----------------------------------------------------------------------------
// GPCP CGRA - N x N mesh of PEs with nearest-neighbour links
//
// The west boundary of PE(0,0) is driven by the streaming input `din`, which
// lets kernels shift a sample stream through the array along a snake path.
// Host access is decoded here so that the top level stays simple.
// -----------------------------------------------------------------------------
`ifndef CGRA_ARRAY_SV
`define CGRA_ARRAY_SV

module cgra_array (
    input  logic                                 clk,
    input  logic                                 rst_n,
    input  logic                                 en,
    input  logic [cgra_pkg::CGRA_NPES-1:0][31:0] inst,
    // host write port (single-cycle pulse)
    input  logic                                 wr_en,
    input  logic [15:0]                          wr_addr,
    input  logic [31:0]                          wr_data,
    // host read port (combinational)
    input  logic [15:0]                          rd_addr,
    output logic [31:0]                          rd_data,
    // streaming input into the array
    input  logic [31:0]                          din
);

  import cgra_pkg::*;

  localparam int N    = CGRA_N;
  localparam int NPES = CGRA_NPES;

  logic [31:0] links [0:N-1][0:N-1];           // link outputs of each PE
  logic [31:0] pe_rdata [0:NPES-1];            // host readback of each PE

  // ---------------- host address decode ----------------
  logic [3:0] host_pe;
  logic [3:0] host_reg;       // write register index
  logic [3:0] rd_reg;         // read register index
  logic       host_we_valid;

  always_comb begin
    host_pe       = 4'b0;
    host_reg      = 4'b0;
    rd_reg        = 4'd8;
    host_we_valid = 1'b0;
    if (wr_addr >= REG_PE_BASE && wr_addr < REG_PE_ACC) begin
      // 0x1000 + pe*32 + reg*4, bits [1:0] must be 0
      host_pe       = wr_addr[8:5];
      host_reg      = {1'b0, wr_addr[4:2]};
      host_we_valid = wr_en && (wr_addr[1:0] == 2'b00);
    end else if (wr_addr >= REG_PE_ACC && wr_addr < (REG_PE_ACC + 16'h40)) begin
      // 0x1200 + pe*4  -> accumulator window
      host_pe       = wr_addr[5:2];
      host_reg      = 4'd8;
      host_we_valid = wr_en && (wr_addr[1:0] == 2'b00);
    end

    if (rd_addr >= REG_PE_BASE && rd_addr < REG_PE_ACC)
      rd_reg = {1'b0, rd_addr[4:2]};
    else
      rd_reg = 4'd8;
  end

  // ---------------- PEs ----------------
  genvar gi, gj;
  generate
    for (gi = 0; gi < N; gi++) begin : G_ROW
      for (gj = 0; gj < N; gj++) begin : G_COL
        localparam int PE_ID = gi * N + gj;
        localparam logic [3:0] PE_ID4 = PE_ID;

        logic [31:0] in_n, in_e, in_s, in_w;

        // neighbour wiring; boundary ties to zero except the west edge of (0,0)
        assign in_n = (gi == 0)   ? 32'b0 : links[gi-1][gj];
        assign in_s = (gi == N-1) ? 32'b0 : links[gi+1][gj];
        assign in_w = (gj == 0)   ? ((gi == 0) ? din : 32'b0) : links[gi][gj-1];
        assign in_e = (gj == N-1) ? 32'b0 : links[gi][gj+1];

        cgra_pe u_pe (
            .clk       (clk),
            .rst_n     (rst_n),
            .en        (en),
            .op        (inst[PE_ID][INST_OP_LSB   +: 4]),
            .src_a     (inst[PE_ID][INST_SRCA_LSB +: 4]),
            .src_b     (inst[PE_ID][INST_SRCB_LSB +: 4]),
            .dst       (inst[PE_ID][INST_DST_LSB  +: 3]),
            .imm       (inst[PE_ID][INST_IMM_LSB  +: 16]),
            .in_n      (in_n),
            .in_e      (in_e),
            .in_s      (in_s),
            .in_w      (in_w),
            .host_we   (host_we_valid && (host_pe == PE_ID4)),
            .host_waddr(host_reg),
            .host_wdata(wr_data),
            .host_raddr(rd_reg),
            .host_rdata(pe_rdata[PE_ID]),
            .link_out  (links[gi][gj])
        );
      end
    end
  endgenerate

  // ---------------- host readback mux ----------------
  always_comb begin
    rd_data = 32'b0;
    if (rd_addr >= REG_PE_BASE && rd_addr < REG_PE_ACC)
      rd_data = pe_rdata[rd_addr[8:5]];
    else if (rd_addr >= REG_PE_ACC && rd_addr < (REG_PE_ACC + 16'h40))
      rd_data = pe_rdata[rd_addr[5:2]];
  end

endmodule

`endif
