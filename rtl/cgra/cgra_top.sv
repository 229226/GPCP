// -----------------------------------------------------------------------------
// GPCP CGRA - top level: instruction memory + sequencer + host registers
//              + double-buffered scratchpad + descriptor-driven copy engine
//
// Host register map (byte offsets, see cgra_pkg):
//   0x0000  W: CTRL {start, clear_done}   R: STATUS {cycle, idle, done, busy}
//   0x0004  W: NUM_CYCLES
//   0x0008  R: CYCLE_COUNT
//   0x000C  R: ID {magic, isa_ver, n, n}
//   0x0010  W: DMA_SRC   (source byte address in SoC RAM)
//   0x0014  W: DMA_DST   (scratchpad index, page-relative bank*32+word)
//   0x0018  W: DMA_LEN   (words)
//   0x001C  W: DMA_CTRL {start, dir, clr_done}  R: {busy, done}
//   0x0020  W: BUF_PAGE  (array page select, 0/1)
//   0x0024  W: DMA_PAGE  (copy-engine page select, 0/1)
//   0x1000  PE register window  (pe*32 + reg*4), reg 0..7
//   0x1200  PE accumulator window (pe*4)
//   0x2000  instruction memory (word index = cycle*NPES + pe), 0x2000..0x9FFF
//   0xA000  scratchpad (bank*128 + word*4), 64 banks = 0xA000..0xBFFF
// -----------------------------------------------------------------------------
`ifndef CGRA_TOP_SV
`define CGRA_TOP_SV

// next scratchpad index for the 2D copy walk
function automatic logic [31:0] dma_next(
    input logic [15:0] cur, input logic [15:0] row_cnt,
    input logic [15:0] row, input logic [15:0] stride);
  logic [15:0] next_cur, next_cnt;
  if (row == 16'd0) begin
    next_cur = cur + 16'd1;
    next_cnt = 16'd0;
  end else if (row_cnt == row - 16'd1) begin
    next_cur = cur + stride - row + 16'd1;   // jump to the next row
    next_cnt = 16'd0;
  end else begin
    next_cur = cur + 16'd1;
    next_cnt = row_cnt + 16'd1;
  end
  dma_next = {next_cur, next_cnt};
endfunction

module cgra_top #(
    parameter bit DBG = 1'b0,
    // max AXI beats per burst (1..=128): data beats are streamed, so a burst of
    // N beats costs N cycles and one address handshake
    parameter int DMA_BEATS = 32
) (
    input  logic        clk,
    input  logic        rst_n,
    // host write port (single-cycle pulse)
    input  logic        wr_en,
    input  logic [15:0] wr_addr,
    input  logic [31:0] wr_data,
    // host read port (combinational)
    input  logic [15:0] rd_addr,
    output logic [31:0] rd_data,
    // streaming input into PE(0,0) west boundary
    input  logic [31:0] din,
    // AXI4 master: copy engine (64-bit data, INCR bursts, 8-byte aligned)
    output logic        m_axi_awvalid,
    input  logic        m_axi_awready,
    output logic [3:0]  m_axi_awid,
    output logic [31:0] m_axi_awaddr,
    output logic [7:0]  m_axi_awlen,
    output logic [2:0]  m_axi_awsize,
    output logic [1:0]  m_axi_awburst,
    output logic        m_axi_wvalid,
    input  logic        m_axi_wready,
    output logic [63:0] m_axi_wdata,
    output logic [7:0]  m_axi_wstrb,
    output logic        m_axi_wlast,
    input  logic        m_axi_bvalid,
    output logic        m_axi_bready,
    input  logic [3:0]  m_axi_bid,
    input  logic [1:0]  m_axi_bresp,
    output logic        m_axi_arvalid,
    input  logic        m_axi_arready,
    output logic [3:0]  m_axi_arid,
    output logic [31:0] m_axi_araddr,
    output logic [7:0]  m_axi_arlen,
    output logic [2:0]  m_axi_arsize,
    output logic [1:0]  m_axi_arburst,
    input  logic        m_axi_rvalid,
    output logic        m_axi_rready,
    input  logic [63:0] m_axi_rdata,
    input  logic        m_axi_rlast,
    input  logic [3:0]  m_axi_rid,
    input  logic [1:0]  m_axi_rresp,
    output logic        busy,
    output logic        done,
    output logic [15:0] cycle_o,
    // level interrupt: array done and/or copy-engine done (masked by ENABLE)
    output logic        irq_o,
    // debug only: SoC-side slave state, readable through REG_DBG_STATE[15:12]
    input  logic [3:0]  dbg_soc_state
);

  import cgra_pkg::*;
  import cgra_desc_pkg::*;   // 64B 描述符字段访问器（tools/gen_cgra_desc.py 生成）

  localparam int NPES   = CGRA_NPES;
  localparam int MAXCYC = CGRA_MAX_CYCLES;

  // ---------------- instruction memory ----------------
  // One 512-bit wide word per row instead of a flat 8192x32 array: the array
  // consumes all 16 PE instruction words of a cycle at once (static schedule),
  // so a flat array needs 16 read ports -- in silicon that means 16 replicated
  // macros (16 x 32 KiB).  A wide word needs a single read port; the host
  // write touches one 32-bit slice, which is a masked write (WR_EN bit mask)
  // on the same single write port.
  // Host word index w = (addr - REG_IMEM_BASE) >> 2 -> row w >> 4, lane w[3:0],
  // which keeps the existing software ABI (word cycle*16 + pe).
  localparam int IMEM_ROWS = CGRA_IMEM_WORDS / CGRA_NPES;   // 512 rows
  localparam int IMEM_AW   = $clog2(IMEM_ROWS);             // 9
  logic [CGRA_NPES-1:0][31:0] imem [0:IMEM_ROWS-1];
  logic [CGRA_NPES-1:0][31:0] inst_flat;

  // ---------------- sequencer ----------------
  logic [15:0] num_cycles;
  logic [15:0] cycle_cnt;

  assign cycle_o = cycle_cnt;

  // ---------------- performance counters ----------------
  // 软件写 REG_PERF_CTRL[0] 清一次；读 REG_PERF_BASE + 4*idx。
  logic perf_clear;
  assign perf_clear = wr_en && (wr_addr == REG_PERF_CTRL) && wr_data[0];

  logic        busy_q, dma_busy_q;
  logic [31:0] perf_cycles;
  logic [31:0] perf_array_busy;
  logic [31:0] perf_array_runs;
  logic [31:0] perf_dma_busy;
  logic [31:0] perf_dma_beats;
  logic [31:0] perf_dma_ar_wait;
  logic [31:0] perf_dma_aw_wait;
  logic [31:0] perf_dma_b_wait;
  logic [31:0] perf_dma_data_wait;
  logic [31:0] perf_dma_starts;
  // 互斥状态账本：每拍只归一类（阵列 → DMA → CPU），三者之和 = perf_cycles
  logic [31:0] perf_state_dma;        // 阵列空闲、DMA busy
  logic [31:0] perf_state_dma_wait;   // 上者中 DMA 正在等 AR/AW/B/数据
  logic [31:0] perf_state_cpu;        // 阵列与 DMA 都空闲（CVA6 执行）
  logic [CGRA_NPES-1:0][31:0] perf_pe_active;
  logic [CGRA_NPES-1:0][31:0] perf_pe_mac;
  // array 侧计数器
  logic [31:0] perf_inst_issued, perf_inst_nop, perf_inst_mac, perf_inst_mul;
  logic [31:0] perf_inst_ld, perf_inst_st, perf_inst_alu;
  logic [31:0] perf_spad_read, perf_spad_write;
  logic [31:0] perf_spad_bank_conflict, perf_spad_word_conflict;

  // Synchronous SRAM read with a one-cycle lookahead: the row fetched during
  // cycle n-1 is the word executed in cycle n.  While idle the address stays
  // at row 0, so the edge that raises `busy` also pre-loads word 0 -- a run
  // still takes exactly num_cycles cycles (no extra fetch cycle).
  logic [IMEM_AW-1:0] imem_raddr;
  assign imem_raddr = busy ? (cycle_cnt[IMEM_AW-1:0] + 1'b1) : '0;
  always_ff @(posedge clk) inst_flat <= imem[imem_raddr];

  // ---------------- copy engine state ----------------
  // Descriptor registers (host written) are latched into the *_run copies when
  // a single-shot transfer starts, so software may program the next transfer
  // while the current one is in flight.
  logic [31:0] dma_src;
  logic [15:0] dma_dst;
  logic [15:0] dma_stride;
  logic [15:0] dma_row;
  logic [15:0] dma_len;
  logic [31:0] dma_run_src;
  logic [15:0] dma_run_len;
  logic [15:0] dma_run_stride;
  logic [15:0] dma_run_row;
  logic [15:0] dma_cur;      // current scratchpad index (2D walk)
  logic [15:0] dma_row_cnt;
  logic [15:0] dma_idx;      // word index inside the transfer
  logic        dma_busy, dma_done, dma_dir, dma_irq_evt;
  logic [1:0]  dma_tgt;      // 0 = scratchpad, 1 = IMEM, 2 = 循环旋转表
  logic        buf_page, dma_page;

  // 每 bank 的循环旋转表（由 ring 描述符或主机逐字装载）
  logic [31:0][6:0] rot_tbl;
  logic [5:0]       rot_zc;

  // descriptor ring: descriptors live in SoC RAM (32 bytes each)
  logic [31:0] ring_base;
  logic [15:0] ring_size, ring_head, ring_tail, ring_done;
  logic        ring_en, ring_mode, desc_irq, rd_desc, wr_status;
  logic [31:0] desc_addr;
  // 复合描述符：desc_buf[3] = {en<<31, page<<16, cycles}，传输完成后可选跑一次阵列
  logic        arr_en_q, arr_req;
  logic [1:0]  arr_phase;      // 0=发启动 1=等 busy 2=等 done
  logic        arr_page_q;
  logic [15:0] arr_cycles_q;
  // RING_TAIL may be programmed as "one past the last entry" == ring_size; that
  // is the same slot as 0, so comparisons must be made modulo the ring depth.
  // Without this, a head that wraps back to 0 never matches a tail of ring_size
  // and the engine re-executes the first descriptors of the batch.
  logic [15:0] ring_tail_n;
  assign ring_tail_n = (ring_size != 16'd0 && ring_tail >= ring_size) ? 16'd0 : ring_tail;

  // descriptor buffer: 4 beats = 32 bytes (the only buffering in the engine -
  // data beats are streamed straight into / out of the scratchpad)
  localparam int DESC_BEATS = 8;
  logic [63:0] desc_buf [0:DESC_BEATS-1];
  // 常用描述符字段（组合；字段定义只来自 cgra_desc_pkg，RTL 里不再出现裸切片）
  logic [31:0] d0_src, d0_out_dst;
  logic [15:0] d1_in_len, d1_len_clamped, d4_out_len, d4_out_spad;
  logic        d0_dir, d0_tgt_rot, d4_out_en;
  assign d0_src         = d64_src(desc_buf[0]);
  assign d0_dir         = d64_dir(desc_buf[0]);
  assign d0_tgt_rot     = d64_tgt_rot(desc_buf[0]);
  assign d1_in_len      = d64_in_len(desc_buf[1]);
  assign d1_len_clamped = (d1_in_len > 16'd1024) ? 16'd1024 : d1_in_len;
  assign d4_out_en      = d64_out_en(desc_buf[4]);
  assign d4_out_len     = d64_out_len(desc_buf[4]);
  assign d4_out_spad    = d64_out_spad(desc_buf[4]);
  assign d0_out_dst     = d64_out_dst(desc_buf[5]);
  logic [2:0]  dma_beat;      // descriptor beat during the fetch
  logic        desc64;        // REG_DESC_MODE[0]：64 字节复合描述符
  logic        out_en_q, arr_run_q, out_run_q, arr_en_desc_q;
  logic [15:0] out_len_q, out_spad_q, out_step_q, in_step_q, rep_left, in_len_q;
  logic        in_dir_q;
  logic [31:0] out_dst_q, in_src_q;
  logic [7:0]  dma_nb;        // beats in the current data burst
  logic [7:0]  dma_bcnt;      // beat counter inside a data burst

  // 2D walk: two scratchpad indices per beat (low / high word of the beat)
  logic [15:0] dma_nxt_cur, dma_nxt_cnt, dma_nxt2_cur, dma_nxt2_cnt;
  always_comb begin
    {dma_nxt_cur,  dma_nxt_cnt}  = dma_next(dma_cur, dma_row_cnt, dma_run_row, dma_run_stride);
    {dma_nxt2_cur, dma_nxt2_cnt} = dma_next(dma_nxt_cur, dma_nxt_cnt, dma_run_row, dma_run_stride);
  end

  // burst sizing for the words that are left (clamped to the 4 KiB page so the
  // INCR burst never crosses an AXI 4 KiB boundary, and to DMA_BEATS)
  // 规划只在 cgra_pkg::dma_plan_burst 里实现一次（长度 / 奇偶 / 4 KiB 裁剪）。
  logic [15:0] dma_bw, dma_nidx;
  // registered burst plan: the burst length used to be derived combinationally
  // from (run_len - idx) and (src + idx*4), which was the critical path in
  // synthesis (idx -> subtract -> <=32 -> mux -> 4 KiB edge -> mux -> nb).
  // The two operands are now updated only when idx changes (burst start or
  // burst end), so the same arithmetic sits on a register-to-register path and
  // the burst path itself is just a compare + mux.
  logic [15:0] dma_wleft_q;
  logic [11:0] dma_baddr_lo_q;
  // "this burst is the last one of the transfer" was the next critical path
  // (idx + bw >= run_len feeding the FSM).  It is now evaluated during the
  // address phase (D_AR/D_AW, at least one cycle before the first data beat).
  logic        dma_last_q;
  logic [31:0] dma_baddr;
  logic        dma_odd, dma_last_odd;
  // 突发规划的单一出口
  dma_burst_t  dma_bp;
  // copy-engine FSM state (declared before dstate_q: strict SV frontends
  // reject the forward reference that Verilator tolerated)
  typedef enum logic [3:0] {
    D_IDLE     = 4'd0,
    D_AR       = 4'd1,   // issue the read address (AR) for a burst
    D_RDESC    = 4'd2,   // collect the 4-beat descriptor
    D_DESC_SET = 4'd3,   // ring: latch the fetched descriptor
    D_SIN      = 4'd4,   // streaming in: each R beat goes straight into the spad
    D_AW       = 4'd5,   // issue the write address (AW)
    D_SOUT     = 4'd6,   // streaming out: each W beat comes straight from the spad
    D_BRESP    = 4'd7,   // wait for the write response
    D_ARR      = 4'd8,   // 复合描述符：传输完成后跑一次阵列
    D_RETIRE   = 4'd9,   // 描述符退休（ring_done / ring_head 记账）
    D_NEXT     = 4'd10,  // 复合描述符：决定下一步跑阵列、搬出还是收尾
    D_OUT      = 4'd11   // 复合描述符：启动搬出
  } dstate_e;
  dstate_e dstate;
  logic [3:0]  dstate_q;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) dstate_q <= D_IDLE; else dstate_q <= dstate;
  assign dma_baddr     = dma_run_src + {16'b0, dma_idx} * 4;
  // nb / odd / bw 全部来自同一个规划函数（读、写通道共用）
  assign dma_bp        = dma_plan_burst(dma_wleft_q, dma_baddr_lo_q, DMA_BEATS);
  assign dma_nb        = dma_bp.nb;
  assign dma_odd       = dma_bp.odd;
  assign dma_bw        = dma_bp.bw;
  assign dma_nidx      = dma_idx + dma_bw;
  assign dma_last_odd  = dma_odd && (dma_bcnt == dma_nb - 8'd1);

  // ---------------- interrupt status / enable ----------------
  // sticky status bits, cleared by writing 1 to REG_IRQ_STATUS; the line is
  // level sensitive so the PLIC sees it until software acknowledges
  logic [1:0]  irq_status, irq_enable;
  logic        done_q, dma_done_q;
  logic [1:0]  irq_evts;

  assign irq_o  = |(irq_status & irq_enable);
  assign irq_evts = {dma_irq_evt, done & ~done_q};

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      irq_status  <= 2'b0;
      irq_enable  <= 2'b0;
      done_q      <= 1'b0;
      dma_done_q  <= 1'b0;
    end else begin
      done_q     <= done;
      dma_done_q <= dma_done;
      if (wr_en && (wr_addr == REG_IRQ_ENABLE)) irq_enable <= wr_data[1:0];
      // W1C clear must not swallow an event raised in the same cycle
      irq_status <= ((wr_en && (wr_addr == REG_IRQ_STATUS)) ? (irq_status & ~wr_data[1:0])
                                                            : irq_status) | irq_evts;
    end
  end

  // scratchpad side of the copy engine: a beat carries two words, so one write
  // port per word (they are different words of the same bank) and two read ports
  logic        spad_dma_we0, spad_dma_we1;
  logic [9:0]  spad_dma_addr0, spad_dma_addr1;
  logic [31:0] spad_dma_wdata0, spad_dma_wdata1;
  logic [9:0]  spad_rd_addr0, spad_rd_addr1;
  logic [31:0] spad_rd_data0, spad_rd_data1;

  // the beat is consumed only on the handshake (valid && ready), never on valid alone
  // dma_tgt = IMEM: the same beat stream feeds the instruction memory instead of
  // the scratchpad (bulk program load; see docs/02 §3.1).
  assign spad_dma_we0    = (dstate == D_SIN) && m_axi_rvalid && m_axi_rready && (dma_tgt == 2'd0);
  assign spad_dma_addr0  = dma_cur[9:0];
  assign spad_dma_wdata0 = m_axi_rdata[31:0];
  assign spad_dma_we1    = (dstate == D_SIN) && m_axi_rvalid && m_axi_rready && (dma_tgt == 2'd0) && !dma_last_odd;
  assign spad_dma_addr1  = dma_nxt_cur[9:0];
  assign spad_dma_wdata1 = m_axi_rdata[63:32];
  assign spad_rd_addr0   = dma_cur[9:0];
  assign spad_rd_addr1   = dma_nxt_cur[9:0];

  // IMEM side of the copy engine: one 8-byte beat carries two instruction words
  logic        dma_imem_we;
  logic [15:0] dma_imem_idx, dma_imem_nidx;
  assign dma_imem_we   = (dstate == D_SIN) && m_axi_rvalid && m_axi_rready && (dma_tgt == 2'd1);
  assign dma_imem_idx  = dma_cur;       // word index 0..8191 (row = idx>>4)
  assign dma_imem_nidx = dma_nxt_cur;

  // 旋转表 side：一个 64-bit beat 装两个 7-bit 条目（低字在前）
  logic        dma_tbl_we;
  assign dma_tbl_we = (dstate == D_SIN) && m_axi_rvalid && m_axi_rready && (dma_tgt == 2'd2);

  // ---------------- 输出写窗口（DMA 的“地址空间保护”） ----------------
  // 描述符只允许写自己声明的窗口 [win_lo, win_hi)。任何越过窗口的写拍被丢弃
  // （wstrb=0）并置 sticky 错误位——即使描述符/突发规划算错，也写不坏相邻缓冲区。
  logic [31:0] win_lo, win_hi;
  logic        win_err, win_bad;
  logic [31:0] beat_addr;
  assign beat_addr = dma_baddr + {23'b0, dma_bcnt} * 32'd8;
  // 奇数字数的末拍只写低 4 字节（wstrb=0x0F），窗口判定要按 4 字节算，
  // 否则会把合法的“奇尾”判成越界、丢掉最后一个字。
  assign win_bad   = (dstate == D_SOUT) && !wr_status &&
                     ((beat_addr < win_lo) ||
                      ((beat_addr + (dma_last_odd ? 32'd4 : 32'd8)) > win_hi));

  // 突发字节数（给下面的断言用）
  logic [31:0] aw_bytes, ar_bytes;
  assign aw_bytes = 32'd8 * ({24'b0, m_axi_awlen} + 32'd1);
  assign ar_bytes = 32'd8 * ({24'b0, m_axi_arlen} + 32'd1);

  // ---------------- AXI4 master ----------------
  always_comb begin
    // read channel: one INCR burst per descriptor chunk (data beats are streamed)
    // AXI rule: once asserted, a valid (and its payload) is held until the
    // partner's ready is seen - here every strobe is a function of the state
    // register only, and the state advances exclusively on valid && ready, so
    // the hold requirement is structural.  Ready may come and go freely; the
    // payload never changes while a valid is waiting.
    m_axi_arvalid = (dstate == D_AR);
    m_axi_arid    = 4'd0;
    m_axi_araddr  = rd_desc ? (ring_base + (desc64 ? {ring_head, 6'b0}
                                                          : {ring_head, 5'b0}))
                                    : dma_baddr;
    m_axi_arlen   = rd_desc ? (desc64 ? 8'd7 : 8'd3) : (dma_nb - 8'd1);
    m_axi_arsize  = 3'b011;      // 8 bytes per beat
    m_axi_arburst = 2'b01;       // INCR
    m_axi_rready  = (dstate == D_RDESC) || (dstate == D_SIN);

    // write channel
    m_axi_awvalid = (dstate == D_AW);
    m_axi_awid    = 4'd0;
    m_axi_awaddr  = wr_status ? (desc_addr + 32'd16) : dma_baddr;
    m_axi_awlen   = wr_status ? 8'd0 : (dma_nb - 8'd1);
    m_axi_awsize  = 3'b011;
    m_axi_awburst = 2'b01;

    m_axi_wvalid  = (dstate == D_SOUT);
    m_axi_wdata   = wr_status ? {32'b0, DESC_STATUS_MAGIC[31:16], dma_run_len}
                              : {spad_rd_data1, spad_rd_data0};
    // 越窗的整拍丢弃（wstrb 全 0 = 不写任何字节）
    m_axi_wstrb   = win_bad ? 8'h00
                            : ((wr_status || dma_last_odd) ? 8'h0F : 8'hFF);
    m_axi_wlast   = wr_status ? 1'b1 : (dma_bcnt == dma_nb - 8'd1);
    m_axi_bready  = (dstate == D_BRESP);
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      dstate      <= D_IDLE;
      dma_busy    <= 1'b0;
      dma_done    <= 1'b0;
      dma_irq_evt <= 1'b0;
      dma_tgt     <= 2'd0;
      dma_idx     <= 16'd0;
      dma_src     <= 32'b0;
      dma_dst     <= 16'b0;
      dma_cur     <= 16'b0;
      dma_row_cnt <= 16'b0;
      dma_stride  <= 16'd1;
      dma_row     <= 16'd0;
      dma_len     <= 16'd0;
      dma_run_src    <= 32'b0;
      dma_run_len    <= 16'd0;
      dma_wleft_q    <= 16'd0;
      dma_baddr_lo_q <= 12'd0;
      dma_last_q     <= 1'b0;
      dma_run_stride <= 16'd1;
      dma_run_row    <= 16'd0;
      dma_dir     <= 1'b0;
      dma_beat    <= 2'd0;
      dma_bcnt    <= 8'd0;
      dma_page    <= 1'b0;
      dma_tgt     <= 2'd0;
      rot_zc      <= 6'd32;
      win_lo      <= 32'b0;
      win_hi      <= 32'hFFFF_FFFF;
      win_err     <= 1'b0;
      for (int i = 0; i < ROT_ENTRIES; i++) rot_tbl[i] <= 7'd0;
      ring_base   <= 32'b0;
      ring_size   <= 16'd0;
      ring_head   <= 16'd0;
      ring_tail   <= 16'd0;
      ring_done   <= 16'd0;
      ring_en     <= 1'b0;
      ring_mode   <= 1'b0;
      desc_irq    <= 1'b0;
      desc_addr   <= 32'b0;
      rd_desc     <= 1'b0;
      wr_status   <= 1'b0;
      for (int i = 0; i < DESC_BEATS; i++) desc_buf[i] <= 64'b0;
    end else begin
      dma_irq_evt <= 1'b0;
      if (win_bad) win_err <= 1'b1;

      if (DBG && m_axi_arvalid && m_axi_arready)
        $display("[cg-dma] AR addr=%h len=%0d rd_desc=%b", m_axi_araddr, m_axi_arlen, rd_desc);
      if (DBG && (dstate != dstate_q))
        $display("[cg-fsm] %0d->%0d wr_st=%b bcnt=%0d nb=%0d idx=%0d",
                 dstate_q, dstate, wr_status, dma_bcnt, dma_nb, dma_idx);
      if (DBG && m_axi_awvalid && m_axi_awready)
        $display("[cg-dma] AW addr=%h len=%0d status=%b", m_axi_awaddr, m_axi_awlen, wr_status);

      unique case (dstate)
        D_IDLE: begin
          // ring mode: fetch the next descriptor as soon as there is work
          if (ring_en && !dma_busy && (ring_size != 16'd0) && (ring_head != ring_tail_n)) begin
            dma_busy  <= 1'b1;
            dma_done  <= 1'b0;
            dma_beat  <= 3'd0;
            dma_bcnt  <= 8'd0;
            ring_mode <= 1'b1;
            rd_desc   <= 1'b1;
            dstate    <= D_AR;
          end
        end
        D_AR: begin
          dma_last_q <= !rd_desc && (dma_wleft_q <= dma_bw);
          if (m_axi_arvalid && m_axi_arready) begin
            dma_beat <= 3'd0;
            dma_bcnt <= 8'd0;
            dstate   <= rd_desc ? D_RDESC : D_SIN;
          end
        end
        D_RDESC: begin
          if (m_axi_rvalid && m_axi_rready) begin
            desc_buf[dma_beat] <= m_axi_rdata;
            if (m_axi_rlast) dstate <= D_DESC_SET;
            else             dma_beat <= dma_beat + 3'd1;
          end
        end
        D_DESC_SET: begin
          dma_run_src    <= d0_src;
          dma_run_len    <= d1_len_clamped;
          dma_wleft_q    <= d1_len_clamped;
          dma_baddr_lo_q <= d0_src[11:0];
          dma_run_stride <= d64_stride(desc_buf[1]);
          dma_run_row    <= d64_row(desc_buf[1]);
          dma_cur        <= d64_spad(desc_buf[0]);
          dma_page       <= d64_page(desc_buf[0]);
          dma_dir        <= d0_dir;
          desc_irq       <= d64_irq(desc_buf[0]);
          /* 描述符发起的传输默认写 scratchpad；tgt_rot 置位时改为装旋转表。
           * dma_tgt 只由寄存器 START 的 CTRL[3] 置位，若不清，前面跑过
           * gpcp_cgra_imem_load（IMEM 目标）的程序会把之后所有 DMA 静默改道
           * 去写 IMEM，阵列只看到旧 spad 数据。 */
          dma_tgt        <= d0_tgt_rot ? 2'd2 : 2'd0;
          /* 方向 = spad→RAM 的描述符：窗口就是 [src, src+len*4)，写拍只允许落在里面。
           * 复合描述符的搬出段会在 D_OUT 里重新装载。 */
          if (d0_dir) begin
            win_lo <= d0_src;
            win_hi <= d0_src + ({16'b0, d1_len_clamped} << 2);
          end
          desc_addr      <= ring_base + (desc64 ? {ring_head, 6'b0}
                                                 : {ring_head, 5'b0});
          in_len_q       <= d1_in_len;
          in_src_q       <= d0_src;
          in_dir_q       <= d0_dir;
          out_en_q       <= desc64 ? d4_out_en   : 1'b0;
          out_len_q      <= desc64 ? d4_out_len  : 16'd0;
          out_spad_q     <= desc64 ? d4_out_spad : 16'd0;
          out_dst_q      <= desc64 ? d0_out_dst : 32'd0;
          rep_left       <= (desc64 && (d64_rep(desc_buf[6]) != 16'd0))
                          ? d64_rep(desc_buf[6]) : 16'd1;
          in_step_q      <= desc64 ? d64_in_step(desc_buf[6])  : 16'd0;
          out_step_q     <= desc64 ? d64_out_step(desc_buf[6]) : 16'd0;
          arr_run_q      <= 1'b0;
          out_run_q      <= 1'b0;
          arr_en_q       <= d64_arr_en(desc_buf[3]);
          arr_en_desc_q  <= d64_arr_en(desc_buf[3]);
          arr_page_q     <= d64_arr_page(desc_buf[3]);
          arr_cycles_q   <= (d64_cycles(desc_buf[3]) > 16'(MAXCYC))
                          ? 16'(MAXCYC) : d64_cycles(desc_buf[3]);
          dma_idx        <= 16'd0;
          dma_row_cnt    <= 16'd0;
          dma_bcnt       <= 8'd0;
          rd_desc        <= 1'b0;
          dstate         <= d0_dir ? D_AW : D_AR;
        end
        D_SIN: begin
          // streaming in: the beat that arrives is written to the scratchpad
          // in the same cycle (two words per beat)
          if (m_axi_rvalid && m_axi_rready) begin
            if (DBG) $display("[cg-walk] cur=%0d cnt=%0d row=%0d stride=%0d odd=%b we0=%b a0=%0d d0=%h a1=%0d d1=%h",
                              dma_cur, dma_row_cnt, dma_run_row, dma_run_stride, dma_last_odd,
                              spad_dma_we0, spad_dma_addr0, spad_dma_wdata0[15:0],
                              spad_dma_addr1, spad_dma_wdata1[15:0]);
            if (m_axi_rlast) begin
              dma_bcnt <= 8'd0;
              dma_idx  <= dma_nidx;
              dma_wleft_q    <= dma_wleft_q - dma_bw;
              dma_baddr_lo_q <= (dma_baddr_lo_q + {4'b0, dma_bw[9:0], 2'b00}) & 12'hFFF;
              if (dma_last_q) begin
                if (ring_mode) begin
                  if (arr_en_q || out_en_q) dstate <= D_NEXT;
                  else begin wr_status <= 1'b1; dstate <= D_AW; end
                end else begin
                  dma_busy    <= 1'b0;
                  dma_done    <= 1'b1;
                  dstate      <= D_IDLE;
                  dma_irq_evt <= 1'b1;
                end
              end else begin
                dstate <= D_AR;
              end
            end else begin
              dma_bcnt <= dma_bcnt + 8'd1;
            end
            if (dma_last_odd) {dma_cur, dma_row_cnt} <= {dma_nxt_cur,  dma_nxt_cnt};
            else              {dma_cur, dma_row_cnt} <= {dma_nxt2_cur, dma_nxt2_cnt};
          end
        end
        D_AW: begin
          dma_last_q <= dma_wleft_q <= dma_bw;
          if (m_axi_awvalid && m_axi_awready) begin
            dma_bcnt <= 8'd0;
            dstate   <= D_SOUT;
          end
        end
        D_SOUT: begin
          // streaming out: each accepted W beat is read from the scratchpad
          if (m_axi_wvalid && m_axi_wready) begin
            dma_bcnt <= dma_bcnt + 8'd1;
            if (dma_last_odd) {dma_cur, dma_row_cnt} <= {dma_nxt_cur,  dma_nxt_cnt};
            else              {dma_cur, dma_row_cnt} <= {dma_nxt2_cur, dma_nxt2_cnt};
            if (m_axi_wlast) dstate <= D_BRESP;
          end
        end
        D_BRESP: begin
          if (m_axi_bvalid && m_axi_bready) begin
            if (wr_status) begin
              // completion status of a ring descriptor
              wr_status <= 1'b0;
              if (desc_irq) dma_irq_evt <= 1'b1;
              /* 复合描述符的「搬入 → 阵列 → 搬出」已经在 D_NEXT/D_ARR 里走完，
               * 这里状态字也写完了，直接退休。
               * 以前这里还会看 arr_en_q 再进一次 D_ARR：D_NEXT 进阵列时并不清
               * arr_en_q，于是同一条描述符会把阵列跑第二遍（PERF_ARRAY_BUSY
               * 恰好是软件预期的 2 倍，白烧一半阵列周期）。 */
              arr_en_q <= 1'b0;
              dstate   <= D_RETIRE;
            end else begin
              dma_idx <= dma_nidx;
              dma_wleft_q    <= dma_wleft_q - dma_bw;
              dma_baddr_lo_q <= (dma_baddr_lo_q + {4'b0, dma_bw[9:0], 2'b00}) & 12'hFFF;
              if (dma_last_q) begin
                if (ring_mode) begin
                  if (arr_en_q || out_en_q) dstate <= D_NEXT;
                  else begin wr_status <= 1'b1; dstate <= D_AW; end
                end else begin
                  dma_busy    <= 1'b0;
                  dma_done    <= 1'b1;
                  dstate      <= D_IDLE;
                  dma_irq_evt <= 1'b1;
                end
              end else begin
                dstate <= D_AW;
              end
            end
          end
        end
        D_ARR: begin
          // 三段式：发启动 → 等 busy 起来（启动真的生效）→ 等 done。
          // 不能直接看 done：上一次运行的 done 还是 1，会立刻误判完成。
          case (arr_phase)
            2'd0: begin
              arr_req   <= 1'b1;
              arr_phase <= 2'd1;
            end
            2'd1: begin
              arr_req <= 1'b0;
              if (busy) arr_phase <= 2'd2;
            end
            default: begin
              if (done && !busy) begin
                arr_phase <= 2'd0;
                arr_run_q <= 1'b1;
                dstate    <= D_NEXT;
              end
            end
          endcase
        end
        D_NEXT: begin
          // 复合描述符：搬入 → 阵列 → 搬出 → 写状态 → 退休/重复
          if (arr_en_q && !arr_run_q)        dstate <= D_ARR;
          else if (out_en_q && !out_run_q)   dstate <= D_OUT;
          else if (rep_left > 16'd1)         dstate <= D_RETIRE;  // 中间轮次不写状态
          else begin wr_status <= 1'b1; dstate <= D_AW; end
        end
        D_OUT: begin
          // 启动搬出：spad 起点/长度来自 w4，RAM 目标来自 w5
          dma_run_src    <= out_dst_q;
          dma_cur        <= out_spad_q;
          dma_run_len    <= out_len_q;
          dma_wleft_q    <= out_len_q;
          dma_baddr_lo_q <= out_dst_q[11:0];
          win_lo         <= out_dst_q;
          win_hi         <= out_dst_q + ({16'b0, out_len_q} << 2);
          dma_dir        <= 1'b1;
          dma_idx        <= 16'd0;
          dma_row_cnt    <= 16'd0;
          dma_bcnt       <= 8'd0;
          out_run_q      <= 1'b1;
          dstate         <= D_AW;
        end
        D_RETIRE: begin
          if (rep_left > 16'd1) begin
            rep_left       <= rep_left - 16'd1;
            /* D_OUT 会把 dma_run_src 改成搬出目标，所以搬入基址单独记在
             * in_src_q；这里从它推进，不能从 dma_run_src 推。 */
            in_src_q       <= in_src_q + {14'b0, in_step_q, 2'b00};
            dma_run_src    <= in_src_q + {14'b0, in_step_q, 2'b00};
            out_dst_q      <= out_dst_q + {14'b0, out_step_q, 2'b00};
            dma_cur        <= desc_buf[0][47:32];
            dma_row_cnt    <= 16'd0;
            dma_idx        <= 16'd0;
            dma_run_len    <= in_len_q;
            dma_wleft_q    <= in_len_q;
            dma_dir        <= in_dir_q;
            dma_baddr_lo_q <= (in_src_q + {14'b0, in_step_q, 2'b00}) & 12'hFFF;
            arr_run_q      <= 1'b0;
            out_run_q      <= 1'b0;
            /* D_BRESP 进阵列前会把 arr_en_q 清掉，repeat 必须重新武装，
             * 否则第 2..N 轮只搬数据、阵列根本不跑。 */
            arr_en_q       <= arr_en_desc_q;
            dstate         <= in_dir_q ? D_AW : D_AR;
          end else begin
          ring_done <= ring_done + 16'd1;
          ring_head <= (ring_head + 16'd1 >= ring_size) ? 16'd0 : ring_head + 16'd1;
          if (ring_en && (((ring_head + 16'd1 >= ring_size) ? 16'd0
                                                         : ring_head + 16'd1) != ring_tail_n)) begin
            dma_beat <= 3'd0;
            rd_desc  <= 1'b1;
            dstate   <= D_AR;
          end else begin
            dma_busy <= 1'b0;
            dstate   <= D_IDLE;
          end
          end
        end
        default: dstate <= D_IDLE;
      endcase
    if (wr_en) begin
      if ((wr_addr >= REG_ROT_BASE) &&
          (wr_addr < (REG_ROT_BASE + 16'(ROT_ENTRIES * 4)))) begin
        if (wr_addr[1:0] == 2'b00)
          rot_tbl[(wr_addr - REG_ROT_BASE) >> 2] <= wr_data[6:0];
      end else begin
      case (wr_addr)
        REG_ROT_ZC:     rot_zc     <= (wr_data[5:0] > 6'd32) ? 6'd32 : wr_data[5:0];
        REG_DESC_MODE:  desc64     <= wr_data[0];
        REG_WIN_STATUS: win_err    <= wr_data[0] ? 1'b0 : win_err;
        REG_DMA_SRC:    dma_src    <= wr_data;
        REG_DMA_DST:    dma_dst    <= wr_data[15:0];
        REG_DMA_LEN:    dma_len    <= (wr_data[15:0] > 16'd1024) ? 16'd1024 : wr_data[15:0];
        REG_DMA_STRIDE: dma_stride <= wr_data[15:0];
        REG_DMA_ROW:    dma_row    <= wr_data[15:0];
        REG_DMA_PAGE:   dma_page   <= wr_data[0];
        REG_BUF_PAGE:   ;   // handled by the sequencer block (single driver)
        REG_RING_BASE:  ring_base  <= wr_data;
        REG_RING_SIZE:  ring_size  <= wr_data[15:0];
        REG_RING_HEAD:  ring_head  <= wr_data[15:0];
        REG_RING_TAIL:  ring_tail  <= wr_data[15:0];
        REG_RING_CTRL:  ring_en    <= wr_data[0];
        REG_DMA_CTRL: begin
          if (wr_data[2]) dma_done <= 1'b0;
          if (wr_data[0] && !dma_busy && (dma_len != 16'd0)) begin
            dma_busy   <= 1'b1;
            dma_done   <= 1'b0;
            dma_idx    <= 16'd0;
            dma_cur    <= dma_dst;
            dma_row_cnt<= 16'd0;
            dma_beat   <= 2'd0;
            dma_bcnt   <= 8'd0;
            dma_dir    <= wr_data[1];
            dma_tgt    <= wr_data[3] ? 2'd1 : 2'd0;   // [3] = 1: 目标是 IMEM（批量装程序）
            ring_mode  <= 1'b0;
            rd_desc    <= 1'b0;
            wr_status  <= 1'b0;
            // snapshot the descriptor for this transfer
            dma_run_src    <= dma_src;
            dma_run_len    <= dma_len;
            dma_run_stride <= dma_stride;
            dma_run_row    <= dma_row;
            dma_wleft_q    <= dma_len;          // idx = 0 after this edge
            dma_baddr_lo_q <= dma_src[11:0];
            if (wr_data[1]) begin               // spad→RAM：窗口 = [src, src+len*4)
              win_lo <= dma_src;
              win_hi <= dma_src + ({16'b0, dma_len} << 2);
            end
            dstate <= wr_data[1] ? D_AW : D_AR;
          end
        end
        default: ;
      endcase
      end
    end
    end
  end

  // ---------------- 事务级断言：长度 / 窗口 / 4 KiB 的可执行契约 ----------------
  // 只在仿真里生效（综合忽略）。任何一条触发都说明突发规划或描述符越界，
  // 不要等到下游数据算错才发现。
`ifndef SYNTHESIS
  always_ff @(posedge clk) begin
    if (rst_n) begin
      if (m_axi_awvalid && m_axi_awready && !wr_status) begin
        if (m_axi_awaddr[2:0] != 3'b0)
          $error("[cgra] 写地址未 8 字节对齐: addr=%h", m_axi_awaddr);
        if ({20'b0, m_axi_awaddr[11:0]} + aw_bytes > 32'd4096)
          $error("[cgra] 写突发跨 4 KiB: addr=%h beats=%0d", m_axi_awaddr,
                 {24'b0, m_axi_awlen} + 32'd1);
        // 字数口径（不是字节）：奇数字数的末拍会覆盖 8 字节但只有 4 字节有效，
        // 用字节比较会把合法的“奇尾”误判成越界。
        if (dma_idx + dma_bw > dma_run_len)
          $error("[cgra] 写突发越过 run_len: idx=%0d bw=%0d len=%0d",
                 dma_idx, dma_bw, dma_run_len);
      end
      if (m_axi_arvalid && m_axi_arready && !rd_desc) begin
        if (m_axi_araddr[2:0] != 3'b0)
          $error("[cgra] 读地址未 8 字节对齐: addr=%h", m_axi_araddr);
        if ({20'b0, m_axi_araddr[11:0]} + ar_bytes > 32'd4096)
          $error("[cgra] 读突发跨 4 KiB: addr=%h beats=%0d", m_axi_araddr,
                 {24'b0, m_axi_arlen} + 32'd1);
        if (dma_idx + dma_bw > dma_run_len)
          $error("[cgra] 读突发越过 run_len: idx=%0d bw=%0d len=%0d",
                 dma_idx, dma_bw, dma_run_len);
      end
      if ((dstate == D_SIN) && m_axi_rvalid && m_axi_rready && m_axi_rlast && dma_last_q)
        if (dma_idx + dma_bw != dma_run_len)
          $error("[cgra] 搬入长度不符: idx=%0d bw=%0d len=%0d",
                 dma_idx, dma_bw, dma_run_len);
      if ((dstate == D_BRESP) && m_axi_bvalid && m_axi_bready && dma_last_q && !wr_status)
        if (dma_idx + dma_bw != dma_run_len)
          $error("[cgra] 搬出长度不符: idx=%0d bw=%0d len=%0d",
                 dma_idx, dma_bw, dma_run_len);
      if ((dstate == D_SOUT) && m_axi_wvalid && m_axi_wready && !wr_status && !win_bad)
        if ((m_axi_wstrb != 8'hFF) && !dma_last_odd)
          $error("[cgra] 非末拍出现部分 wstrb: bcnt=%0d nb=%0d", dma_bcnt, dma_nb);
    end
  end
`endif

  // ---------------- array sequencer ----------------
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      num_cycles <= 16'd0;
      cycle_cnt  <= 16'd0;
      busy       <= 1'b0;
      done       <= 1'b0;
      buf_page   <= 1'b0;   // single driver: the copy engine never touches it
      arr_en_q   <= 1'b0;
      arr_req    <= 1'b0;
      arr_phase  <= 2'd0;
      desc64     <= 1'b0;
      out_en_q   <= 1'b0;
      arr_run_q  <= 1'b0;
      out_run_q  <= 1'b0;
      rep_left   <= 16'd1;
    end else begin
      if (wr_en) begin
        if (wr_addr == REG_CTRL) begin
          if (wr_data[1]) done <= 1'b0;
          if (wr_data[0] && !busy && (num_cycles != 16'd0)) begin
            busy      <= 1'b1;
            done      <= 1'b0;
            cycle_cnt <= 16'd0;
          end
        end else if (wr_addr == REG_NUM_CYCLES) begin
          // "run" register: NUM_CYCLES | page<<16 | start<<31.  Doing all three
          // in one store keeps the ABI safe against the CPU merging adjacent
          // 32-bit stores into a single 64-bit AXI write (see docs/02).
          automatic logic [15:0] cyc;
          cyc = (wr_data[15:0] > MAXCYC) ? 16'(MAXCYC) : wr_data[15:0];
          if (DBG)
            $display("[cgra-run] data=%h start=%b busy=%b cyc=%0d", wr_data,
                     wr_data[31], busy, cyc);
          num_cycles <= cyc;
          if (wr_data[31] && !busy && (cyc != 16'd0)) begin
            busy      <= 1'b1;
            done      <= 1'b0;
            cycle_cnt <= 16'd0;
            buf_page  <= wr_data[16];   // page travels with the start
          end
        end else if (wr_addr == REG_BUF_PAGE) begin
          buf_page <= wr_data[0];
        end else if ((wr_addr >= REG_IMEM_BASE) &&
                     (wr_addr < (REG_IMEM_BASE + CGRA_IMEM_WORDS * 4))) begin
          // word index w = (wr_addr - base) >> 2 -> row w >> 4, lane w[3:0]
          imem[((wr_addr - REG_IMEM_BASE) >> 6)][((wr_addr - REG_IMEM_BASE) >> 2) & 16'hF]
              <= wr_data;
        end
      end else if (dma_imem_we) begin
        // 拷贝引擎直接写 IMEM：一个 8B beat 装两条指令（cur 与 cur+1）
        if (DBG) $display("[cgra-imem] dma idx=%0d lo=%h hi=%h", dma_imem_idx,
                          m_axi_rdata[31:0], m_axi_rdata[63:32]);
        imem[dma_imem_idx[12:4]][dma_imem_idx[3:0]] <= m_axi_rdata[31:0];
        if (!dma_last_odd)
          imem[dma_imem_nidx[12:4]][dma_imem_nidx[3:0]] <= m_axi_rdata[63:32];
      end else if (dma_tbl_we) begin
        // 一个 8B beat 装两个 7-bit 旋转条目（低字 = 偶数下标）
        rot_tbl[dma_cur[4:0]]     <= m_axi_rdata[6:0];
        rot_tbl[dma_nxt_cur[4:0]] <= m_axi_rdata[38:32];
      end else if (arr_req && !busy && (arr_cycles_q != 16'd0)) begin
        // 描述符驱动的阵列运行：拍数/page 来自描述符，不走寄存器写入
        num_cycles <= arr_cycles_q;
        busy       <= 1'b1;
        done       <= 1'b0;
        cycle_cnt  <= 16'd0;
        buf_page   <= arr_page_q;
      end else if (busy) begin
        if (cycle_cnt == num_cycles - 16'd1) begin
          busy <= 1'b0;
          done <= 1'b1;
        end else begin
          cycle_cnt <= cycle_cnt + 16'd1;
        end
      end
    end
  end

  // ---------------- PE array ----------------
  logic [31:0] pe_rd;

  cgra_array u_array (
      .clk    (clk),
      .rst_n  (rst_n),
      .en     (busy),
      .inst   (inst_flat),
      .wr_en  (wr_en),
      .wr_addr(wr_addr),
      .wr_data(wr_data),
      .rd_addr(rd_addr),
      .rd_data(pe_rd),
      .din    (din),
      .rot_tbl(rot_tbl),
      .rot_zc (rot_zc),
      .buf_page (buf_page),
      .dma_page (dma_page),
      .dma_we0   (spad_dma_we0),
      .dma_addr0 (spad_dma_addr0),
      .dma_wdata0(spad_dma_wdata0),
      .dma_we1   (spad_dma_we1),
      .dma_addr1 (spad_dma_addr1),
      .dma_wdata1(spad_dma_wdata1),
      .dma_rd_addr0(spad_rd_addr0),
      .dma_rd_data0(spad_rd_data0),
      .dma_rd_addr1(spad_rd_addr1),
      .dma_rd_data1(spad_rd_data1),
      .perf_clear(perf_clear),
      .perf_inst_issued(perf_inst_issued),
      .perf_inst_nop(perf_inst_nop),
      .perf_inst_mac(perf_inst_mac),
      .perf_inst_mul(perf_inst_mul),
      .perf_inst_ld(perf_inst_ld),
      .perf_inst_st(perf_inst_st),
      .perf_inst_alu(perf_inst_alu),
      .perf_spad_read(perf_spad_read),
      .perf_spad_write(perf_spad_write),
      .perf_spad_bank_conflict(perf_spad_bank_conflict),
      .perf_spad_word_conflict(perf_spad_word_conflict),
      .perf_pe_active(perf_pe_active),
      .perf_pe_mac(perf_pe_mac)
  );

  // ---------------- performance counter registers ----------------
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      perf_cycles <= 32'd0;
      perf_array_busy <= 32'd0;
      perf_array_runs <= 32'd0;
      perf_dma_busy <= 32'd0;
      perf_dma_beats <= 32'd0;
      perf_dma_ar_wait <= 32'd0;
      perf_dma_aw_wait <= 32'd0;
      perf_dma_b_wait <= 32'd0;
      perf_dma_data_wait <= 32'd0;
      perf_dma_starts <= 32'd0;
      perf_state_dma <= 32'd0;
      perf_state_dma_wait <= 32'd0;
      perf_state_cpu <= 32'd0;
      busy_q <= 1'b0;
      dma_busy_q <= 1'b0;
    end else if (perf_clear) begin
      perf_cycles <= 32'd0;
      perf_array_busy <= 32'd0;
      perf_array_runs <= 32'd0;
      perf_dma_busy <= 32'd0;
      perf_dma_beats <= 32'd0;
      perf_dma_ar_wait <= 32'd0;
      perf_dma_aw_wait <= 32'd0;
      perf_dma_b_wait <= 32'd0;
      perf_dma_data_wait <= 32'd0;
      perf_dma_starts <= 32'd0;
      perf_state_dma <= 32'd0;
      perf_state_dma_wait <= 32'd0;
      perf_state_cpu <= 32'd0;
      busy_q <= busy;
      dma_busy_q <= dma_busy;
    end else begin
      perf_cycles <= perf_cycles + 32'd1;
      if (busy) perf_array_busy <= perf_array_busy + 32'd1;
      if (busy && !busy_q) begin
        perf_array_runs <= perf_array_runs + 32'd1;
      end
      if (dma_busy) perf_dma_busy <= perf_dma_busy + 32'd1;
      if (dma_busy && !dma_busy_q)
        perf_dma_starts <= perf_dma_starts + 32'd1;
      // 互斥状态（优先级：阵列 > DMA > CPU）
      if (!busy && dma_busy) begin
        perf_state_dma <= perf_state_dma + 32'd1;
        if (((dstate == D_AR) && !m_axi_arready) ||
            ((dstate == D_AW) && !m_axi_awready) ||
            ((dstate == D_BRESP) && !m_axi_bvalid) ||
            ((dstate == D_SIN) && !m_axi_rvalid) ||
            ((dstate == D_SOUT) && !m_axi_wready) ||
            /* 描述符取指也走同一个 AXI 主口：CPU 正在抢总线时这里会长时间
             * 等 rvalid，以前没计入等待，账本上表现为「DMA 传输」变慢。 */
            ((dstate == D_RDESC) && !m_axi_rvalid))
          perf_state_dma_wait <= perf_state_dma_wait + 32'd1;
      end else if (!busy && !dma_busy) begin
        perf_state_cpu <= perf_state_cpu + 32'd1;
      end
      perf_dma_beats <= perf_dma_beats
          + ((m_axi_rvalid && m_axi_rready) ? 32'd1 : 32'd0)
          + ((m_axi_wvalid && m_axi_wready) ? 32'd1 : 32'd0);
      if ((dstate == D_AR) && !m_axi_arready)
        perf_dma_ar_wait <= perf_dma_ar_wait + 32'd1;
      if ((dstate == D_AW) && !m_axi_awready)
        perf_dma_aw_wait <= perf_dma_aw_wait + 32'd1;
      if ((dstate == D_BRESP) && !m_axi_bvalid)
        perf_dma_b_wait <= perf_dma_b_wait + 32'd1;
      if (((dstate == D_SIN) && !m_axi_rvalid) ||
          ((dstate == D_RDESC) && !m_axi_rvalid) ||
          ((dstate == D_SOUT) && !m_axi_wready))
        perf_dma_data_wait <= perf_dma_data_wait + 32'd1;
      busy_q <= busy;
      dma_busy_q <= dma_busy;
    end
  end

  // 统一编址：0..21 为全局/array 计数，22..37 为每 PE 活跃周期，
  // 38..53 为每 PE MAC 周期，54..56 为互斥状态（DMA / DMA 等待 / CPU）。
  logic [31:0] perf_counters [0:PERF_N-1];
  always_comb begin
    for (int i = 0; i < PERF_N; i++) perf_counters[i] = 32'd0;
    perf_counters[0]  = perf_cycles;
    perf_counters[1]  = perf_array_busy;
    perf_counters[2]  = perf_array_runs;
    perf_counters[3]  = perf_inst_issued;
    perf_counters[4]  = perf_inst_nop;
    perf_counters[5]  = perf_inst_mac;
    perf_counters[6]  = perf_inst_mul;
    perf_counters[7]  = perf_inst_ld;
    perf_counters[8]  = perf_inst_st;
    perf_counters[9]  = perf_inst_alu;
    perf_counters[10] = perf_spad_read;
    perf_counters[11] = perf_spad_write;
    perf_counters[12] = perf_spad_bank_conflict;
    perf_counters[13] = perf_spad_word_conflict;
    perf_counters[14] = perf_dma_busy;
    perf_counters[15] = perf_dma_beats;
    perf_counters[16] = perf_dma_ar_wait;
    perf_counters[17] = perf_dma_aw_wait;
    perf_counters[18] = perf_dma_b_wait;
    perf_counters[19] = perf_dma_data_wait;
    perf_counters[20] = perf_dma_starts;
    perf_counters[21] = {16'b0, ring_done};
    perf_counters[54] = perf_state_dma;
    perf_counters[55] = perf_state_dma_wait;
    perf_counters[56] = perf_state_cpu;
    for (int p = 0; p < CGRA_NPES; p++) begin
      perf_counters[22 + p] = perf_pe_active[p];
      perf_counters[38 + p] = perf_pe_mac[p];
    end
  end

  // ---------------- host read mux ----------------
  always_comb begin
    rd_data = 32'b0;
    if ((rd_addr >= REG_ROT_BASE) &&
        (rd_addr < (REG_ROT_BASE + 16'(ROT_ENTRIES * 4)))) begin
      rd_data = {25'b0, rot_tbl[(rd_addr - REG_ROT_BASE) >> 2]};
    end else if (rd_addr == REG_ROT_ZC) begin
      rd_data = {26'b0, rot_zc};
    end else if (rd_addr < 16'h0100) begin
      case (rd_addr[7:0])
        6'h00: rd_data = {cycle_cnt, 13'b0, 1'b0, done, busy};
        6'h04: rd_data = {16'b0, num_cycles};
        6'h08: rd_data = {16'b0, cycle_cnt};
        6'h0C: rd_data = {CGRA_MAGIC, CGRA_ISA_VER, 8'(CGRA_N), 8'(CGRA_N)};
        6'h10: rd_data = dma_src;
        6'h14: rd_data = {16'b0, dma_dst};
        6'h18: rd_data = {16'b0, dma_len};
        6'h1C: rd_data = {30'b0, dma_done, dma_busy};
        6'h20: rd_data = {31'b0, buf_page};
        6'h24: rd_data = {31'b0, dma_page};
        6'h28: rd_data = {16'b0, dma_stride};
        6'h2C: rd_data = {16'b0, dma_row};
        6'h30: rd_data = dma_run_src;               // latched descriptor (debug)
        6'h34: rd_data = {16'b0, dma_run_len};
        6'h38: rd_data = {16'b0, dma_run_stride};
        6'h3C: rd_data = {16'b0, dma_run_row};
        7'h40: rd_data = {30'b0, irq_status};
        7'h44: rd_data = {30'b0, irq_enable};
        7'h48: rd_data = ring_base;
        7'h4C: rd_data = {16'b0, ring_size};
        7'h50: rd_data = {16'b0, ring_head};
        7'h54: rd_data = {16'b0, ring_tail};
        7'h58: rd_data = {29'b0, dma_busy, (ring_head == ring_tail_n), ring_en};
        7'h5C: rd_data = {16'b0, ring_done};
        7'h60: rd_data = {16'b0, dbg_soc_state, wr_status, rd_desc, 2'b0, 4'(dstate),
                          dma_bcnt[3:0]};
        7'h68: rd_data = 32'b0;
        7'h70: rd_data = {31'b0, win_err};
        default: rd_data = 32'b0;
      endcase
    end else if ((rd_addr >= REG_PERF_BASE) &&
                 (rd_addr < (REG_PERF_BASE + PERF_N * 4))) begin
      rd_data = perf_counters[(rd_addr - REG_PERF_BASE) >> 2];
    end else if (rd_addr >= REG_PE_BASE && rd_addr < (REG_PE_ACC + 16'h40)) begin
      rd_data = pe_rd;
    end else if ((rd_addr >= REG_SPAD_BASE) &&
                 (rd_addr < (REG_SPAD_BASE + SPAD_BYTES))) begin
      rd_data = pe_rd;
    end else if ((rd_addr >= REG_IMEM_BASE) &&
                 (rd_addr < (REG_IMEM_BASE + CGRA_IMEM_WORDS * 4))) begin
      // IMEM is write-only for the host: a readback port would make the SRAM
      // 2R1W instead of 1R1W (~1.5x area), and no software reads microcode
      // back (see docs/08).  The write port still lets a loader verify by run.
      rd_data = 32'b0;
    end
  end

endmodule

`endif
