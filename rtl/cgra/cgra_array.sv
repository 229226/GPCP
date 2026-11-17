// -----------------------------------------------------------------------------
// GPCP CGRA - N x N mesh of PEs with nearest-neighbour links
//              + 8 KiB banked scratchpad (L1 data plane support)
//
// Scratchpad: 64 physical banks (2 pages x 32) x 32 words x 32-bit = 8 KiB.
//   - every physical bank is a 1R1W SRAM: one read + one write per cycle
//   - 16 PE ports reach the 32 banks of a page through an address/data
//     crossbar; each PE has one read-data mux (32:1 per page + page select)
//   - same-word readers share the read port (broadcast)
//   - host window: logical bank*128 + word*4, mapped by spad_swz() below
//
// The static schedule must keep every physical bank at <=1 distinct read word
// and <=1 write word per cycle; a simulation-only assertion enforces it.
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
    // 每 bank 的循环旋转表：{[12:8]=写旋转, [6]=keep, [5]=invalid, [4:0]=读旋转}
    // （仅 imm[15]=1 的 LD/ST 使用；读写可不同，见 PDSCH CN/BN 的用法）
    input  logic [31:0][12:0]                    rot_tbl,
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
    input  logic                                 dma_re,   // copy engine is reading spad
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
  // 物理布局（转置 + GF(2) 散列）：
  //   逻辑地址 idx = {LB[4:0], LW[4:0]}（LB = 逻辑 bank，LW = 逻辑字）
  //   phys_bank    = {page, LW ^ M(LB)}
  //   phys_word    = LB
  // M 是 5x5 GF(2) 双射（可逆线性映射，5 个异或项），把逻辑 bank 的 5 个
  // 位混合后异或到物理 bank。转置把“同一逻辑 bank 的 32 个字”摊到 32 个
  // 物理 bank；M 把跨逻辑 bank 的固定步长访问打散。M 由
  // FFT / PDSCH-full / pss_corr / sss_corr 的真实访问序列求解得到
  // （每个物理 bank 每拍 <=1 个不同字），仿真断言见文件末尾。
  logic [31:0] spad [0:BANKS-1][0:SW-1];

  function automatic logic [4:0] spad_hash(input logic [4:0] lb);
    spad_hash = (lb[0] ? 5'd14 : 5'd0) ^ (lb[1] ? 5'd25 : 5'd0) ^
                (lb[2] ? 5'd1  : 5'd0) ^ (lb[3] ? 5'd20 : 5'd0) ^
                (lb[4] ? 5'd11 : 5'd0);
  endfunction

  function automatic logic [4:0] spad_swz(input logic [4:0] lb, input logic [4:0] lw);
    spad_swz = lw ^ spad_hash(lb);
  endfunction

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

  // 旋转只对“带旋转标志且该方向的 rot!=0”的访问生效，其余访问保持原地址。
  // 读、写各用各的 5-bit 偏移：PDSCH 的 CN 读写都用 sh（m' 预移位），
  // BN 读写都用 0；两个核因此都不需要同拍多端口。
  always_comb begin
    for (int p = 0; p < NPES; p++) begin
      automatic logic [4:0] rb = pe_raddr[p][9:5];
      automatic logic [4:0] wb = pe_waddr[p][9:5];
      automatic logic [5:0] rs = {1'b0, pe_raddr[p][4:0]} + {1'b0, rot_tbl[rb][4:0]};
      automatic logic [5:0] ws = {1'b0, pe_waddr[p][4:0]} + {1'b0, rot_tbl[wb][12:8]};
      automatic logic [4:0] rw = (rs >= rot_zc_eff) ? (rs - rot_zc_eff) : rs[4:0];
      automatic logic [4:0] ww = (ws >= rot_zc_eff) ? (ws - rot_zc_eff) : ws[4:0];
      pe_raddr_eff[p] = (pe_rot[p] && (rot_tbl[rb][4:0] != 5'd0)) ? {rb, rw} : pe_raddr[p];
      pe_waddr_eff[p] = (pe_rot[p] && (rot_tbl[wb][12:8] != 5'd0)) ? {wb, ww} : pe_waddr[p];
      pe_we_eff[p]    = pe_we[p] && !(pe_rot[p] && rot_tbl[wb][6]);
    end
  end

  // PE 侧请求（逻辑地址 -> 物理 bank/word）
  logic        pe_rd_v  [0:NPES-1];
  logic [4:0]  pe_rd_pb [0:NPES-1];
  logic [4:0]  pe_rd_pw [0:NPES-1];
  logic        pe_wr_v  [0:NPES-1];
  logic [4:0]  pe_wr_pb [0:NPES-1];
  logic [4:0]  pe_wr_pw [0:NPES-1];

  always_comb begin
    for (int p = 0; p < NPES; p++) begin
      automatic logic [4:0] lb_r = pe_raddr_eff[p][9:5];
      automatic logic [4:0] lw_r = pe_raddr_eff[p][4:0];
      automatic logic [4:0] lb_w = pe_waddr_eff[p][9:5];
      automatic logic [4:0] lw_w = pe_waddr_eff[p][4:0];
      pe_rd_v[p]  = en && (inst[p][INST_OP_LSB +: 4] == OP_LD) &&
                    !(pe_rot[p] && rot_tbl[lb_r][5]);
      pe_rd_pb[p] = spad_swz(lb_r, lw_r);
      pe_rd_pw[p] = lb_r;
      pe_wr_v[p]  = en && pe_we_eff[p];
      pe_wr_pb[p] = spad_swz(lb_w, lw_w);
      pe_wr_pw[p] = lb_w;
    end
  end

  // DMA 侧请求（页相对逻辑地址 -> 物理 bank/word）
  logic [4:0] dma_wr_pb0, dma_wr_pb1, dma_rd_pb0, dma_rd_pb1;
  logic [4:0] dma_wr_pw0, dma_wr_pw1, dma_rd_pw0, dma_rd_pw1;

  assign dma_wr_pb0 = spad_swz(dma_addr0[9:5], dma_addr0[4:0]);
  assign dma_wr_pw0 = dma_addr0[9:5];
  assign dma_wr_pb1 = spad_swz(dma_addr1[9:5], dma_addr1[4:0]);
  assign dma_wr_pw1 = dma_addr1[9:5];
  assign dma_rd_pb0 = spad_swz(dma_rd_addr0[9:5], dma_rd_addr0[4:0]);
  assign dma_rd_pw0 = dma_rd_addr0[9:5];
  assign dma_rd_pb1 = spad_swz(dma_rd_addr1[9:5], dma_rd_addr1[4:0]);
  assign dma_rd_pw1 = dma_rd_addr1[9:5];

  // 1R1W 端口是否超预算（由下面的 spad 仲裁逻辑按物理 bank 给出）
  logic spad_rd_conflict, spad_wr_conflict, spad_same_word_wr;

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

    // 1R1W 端口是否超预算，由下面的 spad 仲裁逻辑按物理 bank 给出
    bank_conflict = spad_wr_conflict || spad_rd_conflict;
    word_conflict = spad_same_word_wr;
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

  // host scratchpad access（逻辑地址按同一 spad_swz 映射到物理 bank）
  logic        host_sp_we;
  logic [5:0]  host_bank;
  logic [4:0]  host_word;
  logic [10:0] host_idx;
  logic        host_rd_v;
  logic [5:0]  host_rd_bank;
  logic [4:0]  host_rd_word;

  always_comb begin
    host_sp_we = 1'b0;
    host_bank  = 6'b0;
    host_word  = 5'b0;
    host_idx   = 11'b0;
    if ((wr_addr >= REG_SPAD_BASE) &&
        (wr_addr < (REG_SPAD_BASE + SPAD_BYTES))) begin
      host_idx   = (wr_addr - REG_SPAD_BASE) >> 2;
      host_bank  = {host_idx[10], spad_swz(host_idx[9:5], host_idx[4:0])};
      host_word  = host_idx[9:5];
      host_sp_we = wr_en && (wr_addr[1:0] == 2'b00);
    end

    host_rd_v    = 1'b0;
    host_rd_bank = 6'b0;
    host_rd_word = 5'd0;
    if ((rd_addr >= REG_SPAD_BASE) &&
        (rd_addr < (REG_SPAD_BASE + SPAD_BYTES)) &&
        (rd_addr[1:0] == 2'b00)) begin
      automatic logic [10:0] ridx = (rd_addr - REG_SPAD_BASE) >> 2;
      host_rd_v    = 1'b1;
      host_rd_bank = {ridx[10], spad_swz(ridx[9:5], ridx[4:0])};
      host_rd_word = ridx[9:5];
    end
  end

  // ---------------- 1R1W bank ports + 16:32 crossbar ----------------
  // 每个物理 bank 一个读地址、一个写地址/数据/使能；写方向按
  // host > DMA0 > DMA1 > PE 优先级选出唯一写者。读方向同优先级选出
  // 唯一地址，数据由所有请求同一 (bank, word) 的 PE 共享（广播）。
  logic [4:0]  bank_rd_addr [0:BANKS-1];
  logic [31:0] bank_rd_data [0:BANKS-1];
  logic [4:0]  bank_wr_addr [0:BANKS-1];
  logic [31:0] bank_wr_data [0:BANKS-1];
  logic        bank_wr_en   [0:BANKS-1];

  always_comb begin
    spad_rd_conflict  = 1'b0;
    spad_wr_conflict  = 1'b0;
    spad_same_word_wr = 1'b0;

    for (int b = 0; b < BANKS; b++) begin
      automatic logic [5:0] bb = b[5:0];
      automatic logic [4:0] pb = b[4:0];
      automatic logic       pg = b[5];
      automatic logic       found;
      automatic logic [4:0] rw;
      automatic logic       rv;
      automatic logic [4:0] ww;
      automatic logic       wv;

      // ---- read port ----
      // 优先级：DMA/PE 先，主机调试读最后（主机读是 best-effort，不能抢
      // 阵列/拷贝引擎的读口；阵列运行时 CPU 读 spad 会读到当前端口的数据）。
      rv = 1'b0;
      rw = 5'd0;
      bank_rd_addr[b] = 5'd0;
      if (dma_re && (dma_page == pg) && (dma_rd_pb0 == pb)) begin
        if (rv && (dma_rd_pw0 != rw)) spad_rd_conflict = 1'b1;
        else begin
          rv = 1'b1;
          rw = dma_rd_pw0;
          bank_rd_addr[b] = dma_rd_pw0;
        end
      end
      if (dma_re && (dma_page == pg) && (dma_rd_pb1 == pb)) begin
        if (rv && (dma_rd_pw1 != rw)) spad_rd_conflict = 1'b1;
        else begin
          rv = 1'b1;
          rw = dma_rd_pw1;
          bank_rd_addr[b] = dma_rd_pw1;
        end
      end
      found = 1'b0;
      for (int p = 0; p < NPES; p++)
        if ((buf_page == pg) && pe_rd_v[p] && (pe_rd_pb[p] == pb)) begin
          if (!found) begin
            found = 1'b1;
            if (rv && (pe_rd_pw[p] != rw)) spad_rd_conflict = 1'b1;
            else begin
              rv = 1'b1;
              rw = pe_rd_pw[p];
              bank_rd_addr[b] = pe_rd_pw[p];
            end
          end else if (pe_rd_pw[p] != rw) begin
            spad_rd_conflict = 1'b1;
          end
        end
      if (!rv && host_rd_v && (host_rd_bank == bb)) begin
        rv = 1'b1;
        rw = host_rd_word;
        bank_rd_addr[b] = host_rd_word;
      end

      // ---- write port ----
      wv = 1'b0;
      ww = 5'd0;
      bank_wr_en[b]   = 1'b0;
      bank_wr_addr[b] = 5'd0;
      bank_wr_data[b] = 32'd0;
      if (host_sp_we && (host_bank == bb)) begin
        wv = 1'b1;
        ww = host_word;
        bank_wr_en[b]   = 1'b1;
        bank_wr_addr[b] = host_word;
        bank_wr_data[b] = wr_data;
      end
      if (dma_we0 && (dma_page == pg) && (dma_wr_pb0 == pb)) begin
        if (wv && (dma_wr_pw0 != ww)) spad_wr_conflict = 1'b1;
        else if (wv) spad_same_word_wr = 1'b1;
        else begin
          wv = 1'b1;
          ww = dma_wr_pw0;
          bank_wr_en[b]   = 1'b1;
          bank_wr_addr[b] = dma_wr_pw0;
          bank_wr_data[b] = dma_wdata0;
        end
      end
      if (dma_we1 && (dma_page == pg) && (dma_wr_pb1 == pb)) begin
        if (wv && (dma_wr_pw1 != ww)) spad_wr_conflict = 1'b1;
        else if (wv) spad_same_word_wr = 1'b1;
        else begin
          wv = 1'b1;
          ww = dma_wr_pw1;
          bank_wr_en[b]   = 1'b1;
          bank_wr_addr[b] = dma_wr_pw1;
          bank_wr_data[b] = dma_wdata1;
        end
      end
      found = 1'b0;
      for (int p = 0; p < NPES; p++)
        if ((buf_page == pg) && pe_wr_v[p] && (pe_wr_pb[p] == pb)) begin
          if (!found) begin
            found = 1'b1;
            if (wv && (pe_wr_pw[p] != ww)) spad_wr_conflict = 1'b1;
            else if (wv) spad_same_word_wr = 1'b1;
            else begin
              wv = 1'b1;
              ww = pe_wr_pw[p];
              bank_wr_en[b]   = 1'b1;
              bank_wr_addr[b] = pe_wr_pw[p];
              bank_wr_data[b] = pe_wdata[p];
            end
          end else if (pe_wr_pw[p] != ww) begin
            spad_wr_conflict = 1'b1;
          end else begin
            spad_same_word_wr = 1'b1;
          end
        end
    end
  end

  // 单写者时钟块 + 每 bank 一个组合读口（1R1W）
  always_ff @(posedge clk) begin
    for (int b = 0; b < BANKS; b++)
      if (bank_wr_en[b]) spad[b][bank_wr_addr[b]] <= bank_wr_data[b];
  end

  always_comb begin
    for (int b = 0; b < BANKS; b++)
      bank_rd_data[b] = spad[b][bank_rd_addr[b]];
  end

  assign dma_rd_data0 = bank_rd_data[{dma_page, dma_rd_pb0}];
  assign dma_rd_data1 = bank_rd_data[{dma_page, dma_rd_pb1}];

`ifndef SYNTHESIS
  // 1R1W 端口超预算：静态调度必须避免；仿真直接报错（硬件不能暂停）。
  always_ff @(posedge clk) begin
    if (rst_n && spad_rd_conflict)
      $error("[cgra] spad 读口冲突：同一物理 bank 两个不同字");
    if (rst_n && spad_wr_conflict)
      $error("[cgra] spad 写口冲突：同一物理 bank 两个不同字");
  end
`endif

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
            : bank_rd_data[{buf_page, pe_rd_pb[PE_ID]}];
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
      rd_data = bank_rd_data[host_rd_bank];
  end

endmodule

`endif
