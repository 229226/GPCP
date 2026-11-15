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
    // 每 bank 的循环旋转表：{keep, valid, rot[4:0]}（仅 imm[15]=1 的 LD/ST 使用）
    input  logic [31:0][6:0]                     rot_tbl,
    input  logic [5:0]                           rot_zc,
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
    output logic [31:0]                          dma_rd_data1,
    // performance counters (clear is a single-cycle pulse from the host)
    input  logic                                 perf_clear,
    output logic [31:0]                          perf_inst_issued,
    output logic [31:0]                          perf_inst_nop,
    output logic [31:0]                          perf_inst_mac,
    output logic [31:0]                          perf_inst_mul,
    output logic [31:0]                          perf_inst_ld,
    output logic [31:0]                          perf_inst_st,
    output logic [31:0]                          perf_inst_alu,
    output logic [31:0]                          perf_spad_read,
    output logic [31:0]                          perf_spad_write,
    output logic [31:0]                          perf_spad_bank_conflict,
    output logic [31:0]                          perf_spad_word_conflict,
    output logic [cgra_pkg::CGRA_NPES-1:0][31:0] perf_pe_active,
    output logic [cgra_pkg::CGRA_NPES-1:0][31:0] perf_pe_mac
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
  logic        pe_rot [0:NPES-1];
  logic [9:0]  pe_raddr_eff [0:NPES-1];
  logic [9:0]  pe_waddr_eff [0:NPES-1];
  logic        pe_we_eff [0:NPES-1];

  // 旋转后的有效地址：word = (word + rot[bank]) mod ZC。
  // word<=31、rot<=31、ZC<=32，所以一次减法足够。
  logic [5:0]  rot_zc_eff;
  assign rot_zc_eff = (rot_zc == 6'd0) ? 6'd32 : rot_zc;

  // 旋转只对“带旋转标志且该 bank 的 rot!=0”的访问生效，其余访问保持原地址。
  always_comb begin
    for (int p = 0; p < NPES; p++) begin
      automatic logic [4:0] rb = pe_raddr[p][9:5];
      automatic logic [4:0] wb = pe_waddr[p][9:5];
      automatic logic [5:0] rs = {1'b0, pe_raddr[p][4:0]} + {1'b0, rot_tbl[rb][4:0]};
      automatic logic [5:0] ws = {1'b0, pe_waddr[p][4:0]} + {1'b0, rot_tbl[wb][4:0]};
      automatic logic [4:0] rw = (rs >= rot_zc_eff) ? (rs - rot_zc_eff) : rs[4:0];
      automatic logic [4:0] ww = (ws >= rot_zc_eff) ? (ws - rot_zc_eff) : ws[4:0];
      pe_raddr_eff[p] = (pe_rot[p] && (rot_tbl[rb][4:0] != 5'd0)) ? {rb, rw} : pe_raddr[p];
      pe_waddr_eff[p] = (pe_rot[p] && (rot_tbl[wb][4:0] != 5'd0)) ? {wb, ww} : pe_waddr[p];
      pe_we_eff[p]    = pe_we[p] && !(pe_rot[p] && rot_tbl[wb][6]);
    end
  end

  // ---------------- performance counters ----------------
  // 每个 array run 周期统计 PE 指令组合、显式 LD/ST 与同拍多写冲突。
  logic [4:0]  n_nop, n_mac, n_mul, n_ld, n_st, n_alu;
  logic        bank_conflict, word_conflict;
  logic [3:0]  perf_op_c, perf_op_f;

  always_comb begin
    n_nop = 5'd0;
    n_mac = 5'd0;
    n_mul = 5'd0;
    n_ld  = 5'd0;
    n_st  = 5'd0;
    n_alu = 5'd0;
    for (int p = 0; p < NPES; p++) begin
      perf_op_c = inst[p][INST_OP_LSB +: 4];
      case (perf_op_c)
        OP_NOP:            n_nop++;
        OP_MAC, OP_MACL:   n_mac++;
        OP_MUL:            n_mul++;
        OP_LD:             n_ld++;
        OP_ST:             n_st++;
        default:           n_alu++;
      endcase
    end

    bank_conflict = 1'b0;
    word_conflict = 1'b0;
    for (int p = 0; p < NPES; p++)
      for (int q = p + 1; q < NPES; q++)
        if (en && pe_we_eff[p] && pe_we_eff[q]) begin
          if (pe_waddr_eff[p][9:5] == pe_waddr_eff[q][9:5])
            bank_conflict = 1'b1;
          if (pe_waddr_eff[p] == pe_waddr_eff[q])
            word_conflict = 1'b1;
        end
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      perf_inst_issued <= 32'd0;
      perf_inst_nop    <= 32'd0;
      perf_inst_mac    <= 32'd0;
      perf_inst_mul    <= 32'd0;
      perf_inst_ld     <= 32'd0;
      perf_inst_st     <= 32'd0;
      perf_inst_alu    <= 32'd0;
      perf_spad_read   <= 32'd0;
      perf_spad_write  <= 32'd0;
      perf_spad_bank_conflict <= 32'd0;
      perf_spad_word_conflict <= 32'd0;
      for (int p = 0; p < NPES; p++) begin
        perf_pe_active[p] <= 32'd0;
        perf_pe_mac[p]    <= 32'd0;
      end
    end else if (perf_clear) begin
      perf_inst_issued <= 32'd0;
      perf_inst_nop    <= 32'd0;
      perf_inst_mac    <= 32'd0;
      perf_inst_mul    <= 32'd0;
      perf_inst_ld     <= 32'd0;
      perf_inst_st     <= 32'd0;
      perf_inst_alu    <= 32'd0;
      perf_spad_read   <= 32'd0;
      perf_spad_write  <= 32'd0;
      perf_spad_bank_conflict <= 32'd0;
      perf_spad_word_conflict <= 32'd0;
      for (int p = 0; p < NPES; p++) begin
        perf_pe_active[p] <= 32'd0;
        perf_pe_mac[p]    <= 32'd0;
      end
    end else if (en) begin
      perf_inst_issued <= perf_inst_issued + 32'(NPES);
      perf_inst_nop    <= perf_inst_nop + {27'd0, n_nop};
      perf_inst_mac    <= perf_inst_mac + {27'd0, n_mac};
      perf_inst_mul    <= perf_inst_mul + {27'd0, n_mul};
      perf_inst_ld     <= perf_inst_ld + {27'd0, n_ld};
      perf_inst_st     <= perf_inst_st + {27'd0, n_st};
      perf_inst_alu    <= perf_inst_alu + {27'd0, n_alu};
      perf_spad_read   <= perf_spad_read + {27'd0, n_ld};
      perf_spad_write  <= perf_spad_write + {27'd0, n_st};
      if (bank_conflict)
        perf_spad_bank_conflict <= perf_spad_bank_conflict + 32'd1;
      if (word_conflict)
        perf_spad_word_conflict <= perf_spad_word_conflict + 32'd1;
      for (int p = 0; p < NPES; p++) begin
        perf_op_f = inst[p][INST_OP_LSB +: 4];
        if (perf_op_f != OP_NOP)
          perf_pe_active[p] <= perf_pe_active[p] + 32'd1;
        if (perf_op_f == OP_MAC || perf_op_f == OP_MACL)
          perf_pe_mac[p] <= perf_pe_mac[p] + 32'd1;
      end
    end
  end

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

  // one clocked block owns the whole scratchpad (single driver for Verilator).
  // Each bank has a write crossbar: every PE (and the two copy-engine ports)
  // may write a *different* word of the same bank in the same cycle.  Two
  // writers to the same word are resolved by priority (lowest PE index wins),
  // which is what a bank with a multi-write crossbar does in hardware; the
  // microcode no longer has to serialise stores that only differ in the word.
  always_ff @(posedge clk) begin
    for (int b = 0; b < BANKS; b++) begin
      if (host_sp_we && (host_bank == b[5:0])) spad[b][host_word] <= wr_data;
      else begin
        if (dma_we0 && (dma_page == b[5]) && (dma_addr0[9:5] == b[4:0]))
          spad[b][dma_addr0[4:0]] <= dma_wdata0;
        else begin
          for (int p = 0; p < NPES; p++)
            if (en && pe_we_eff[p] && (pe_waddr_eff[p][9:5] == b[4:0]) && (buf_page == b[5]))
              spad[b][pe_waddr_eff[p][4:0]] <= pe_wdata[p];
        end
      end
      // second copy-engine port: the two words of a 64-bit beat are distinct
      // words of the same bank, so this is a real dual-write port
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
            .spad_rot  (pe_rot[PE_ID]),
            .spad_raddr(pe_raddr[PE_ID]),
            .spad_rdata(pe_rdata_sp[PE_ID]),
            .spad_we   (pe_we[PE_ID]),
            .spad_waddr(pe_waddr[PE_ID]),
            .spad_wdata(pe_wdata[PE_ID]),
            .link_out  (links[gi][gj])
        );

        // combinational scratchpad read (one read port per PE)
        // bit5 = invalid：该 bank 的 LD 直接返回 0（BN 的缺失边），
        // 不依赖软件清零，也不受 spad 里残留数据影响。
        assign pe_rdata_sp[PE_ID] =
            (pe_rot[PE_ID] && rot_tbl[pe_raddr_eff[PE_ID][9:5]][5]) ? 32'b0
            : spad[{buf_page, pe_raddr_eff[PE_ID][9:5]}][pe_raddr_eff[PE_ID][4:0]];
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
