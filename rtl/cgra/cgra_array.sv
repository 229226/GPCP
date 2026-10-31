// -----------------------------------------------------------------------------
// GPCP CGRA - N x N mesh of PEs with nearest-neighbour links
//              + 32-bank distributed scratchpad (L1 data plane support)
//
// Scratchpad: 32 banks x 32 words x 32-bit (4 KiB).
//   - every PE has one read port (any bank/word) and one write port
//   - bank b normally stores element b (data), banks 16..31 hold per-PE
//     coefficients / twiddles
//   - host window: 0x3000 + (bank*32 + word)*4
//
// The scheduler is responsible for avoiding multiple simultaneous accesses to
// the same bank (the kernels in tools/cgra.py are written with that in mind).
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
    input  logic [31:0]                          din,
    // double buffering: array page / copy-engine page
    input  logic                                 buf_page,
    input  logic                                 dma_page,
    // copy-engine write ports (page-relative indices): a 64-bit beat carries
    // two words, so two ports let the engine store 2 words per cycle
    input  logic                                 dma_we0,
    input  logic [9:0]                           dma_addr0,
    input  logic [31:0]                          dma_wdata0,
    input  logic                                 dma_we1,
    input  logic [9:0]                           dma_addr1,
    input  logic [31:0]                          dma_wdata1,
    // copy-engine read ports (spad -> memory direction)
    input  logic [9:0]                           dma_rd_addr0,
    output logic [31:0]                          dma_rd_data0,
    input  logic [9:0]                           dma_rd_addr1,
    output logic [31:0]                          dma_rd_data1
);

  import cgra_pkg::*;

  localparam int N     = CGRA_N;
  localparam int NPES  = CGRA_NPES;
  localparam int BANKS = SPAD_BANKS;
  localparam int SW    = SPAD_WORDS;

  logic [31:0] links [0:N-1][0:N-1];           // link outputs of each PE
  logic [31:0] pe_rdata [0:NPES-1];            // host readback of each PE

  // ---------------- scratchpad ----------------
  logic [31:0] spad [0:BANKS-1][0:SW-1];

  logic [9:0]  pe_raddr [0:NPES-1];
  logic [31:0] pe_rdata_sp [0:NPES-1];
  logic        pe_we [0:NPES-1];
  logic [9:0]  pe_waddr [0:NPES-1];
  logic [31:0] pe_wdata [0:NPES-1];

  // host scratchpad access
  logic        host_sp_we;
  logic [5:0]  host_bank;
  logic [4:0]  host_word;
  logic [10:0] host_idx;

  always_comb begin
    host_sp_we = 1'b0;
    host_bank  = 6'b0;
    host_word  = 5'b0;
    host_idx   = 11'b0;
    if ((wr_addr >= REG_SPAD_BASE) &&
        (wr_addr < (REG_SPAD_BASE + SPAD_BYTES))) begin
      host_idx  = (wr_addr - REG_SPAD_BASE) >> 2;
      host_bank = host_idx[10:5];
      host_word = host_idx[4:0];
      host_sp_we = wr_en && (wr_addr[1:0] == 2'b00);
    end
  end

  // one clocked block owns the whole scratchpad (single driver for Verilator)
  always_ff @(posedge clk) begin
    for (int b = 0; b < BANKS; b++) begin
      automatic logic        found;
      automatic logic [9:0]  wa;
      automatic logic [31:0] wd;
      found = 1'b0;
      wa    = 10'b0;
      wd    = 32'b0;
      for (int p = 0; p < NPES; p++) begin
        // only the PE instruction of the *running* cycle may write; while the
        // sequencer is idle the last microcode word stays visible, and without
        // this gate an idle array would keep storing into the scratchpad
        if (en && !found && pe_we[p] && (pe_waddr[p][9:5] == b[4:0]) && (buf_page == b[5])) begin
          found = 1'b1;
          wa    = pe_waddr[p];
          wd    = pe_wdata[p];
        end
      end
      if (host_sp_we && (host_bank == b[5:0]))
        spad[b][host_word] <= wr_data;
      else if (dma_we0 && (dma_page == b[5]) && (dma_addr0[9:5] == b[4:0]))
        spad[b][dma_addr0[4:0]] <= dma_wdata0;
      else if (found)
        spad[b][wa[4:0]] <= wd;
      // second copy-engine port: the two words of a 64-bit beat are distinct
      // words of the same bank, so this is a real dual-write port (host keeps
      // priority; a same-word collision would only be a broken descriptor)
      if (dma_we1 && (dma_page == b[5]) && (dma_addr1[9:5] == b[4:0]) &&
          !(host_sp_we && (host_bank == b[5:0])))
        spad[b][dma_addr1[4:0]] <= dma_wdata1;
    end
  end

  assign dma_rd_data0 = spad[{dma_page, dma_rd_addr0[9:5]}][dma_rd_addr0[4:0]];
  assign dma_rd_data1 = spad[{dma_page, dma_rd_addr1[9:5]}][dma_rd_addr1[4:0]];

  // ---------------- host register decode (PE windows) ----------------
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
            .spad_raddr(pe_raddr[PE_ID]),
            .spad_rdata(pe_rdata_sp[PE_ID]),
            .spad_we   (pe_we[PE_ID]),
            .spad_waddr(pe_waddr[PE_ID]),
            .spad_wdata(pe_wdata[PE_ID]),
            .link_out  (links[gi][gj])
        );

        // combinational scratchpad read (one read port per PE)
        assign pe_rdata_sp[PE_ID] =
            spad[{buf_page, pe_raddr[PE_ID][9:5]}][pe_raddr[PE_ID][4:0]];
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
    else if ((rd_addr >= REG_SPAD_BASE) &&
             (rd_addr < (REG_SPAD_BASE + SPAD_BYTES)) &&
             (rd_addr[1:0] == 2'b00))
      rd_data = spad[((rd_addr - REG_SPAD_BASE) >> 2) >> 5]
                     [((rd_addr - REG_SPAD_BASE) >> 2) & 5'h1F];
  end

endmodule

`endif
