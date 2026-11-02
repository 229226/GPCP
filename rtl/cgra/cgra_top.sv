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

  localparam int NPES   = CGRA_NPES;
  localparam int MAXCYC = CGRA_MAX_CYCLES;

  // ---------------- instruction memory ----------------
  logic [31:0] imem [0:CGRA_IMEM_WORDS-1];
  logic [CGRA_NPES-1:0][31:0] inst_flat;

  // ---------------- sequencer ----------------
  logic [15:0] num_cycles;
  logic [15:0] cycle_cnt;

  assign cycle_o = cycle_cnt;

  always_comb begin
    for (int p = 0; p < NPES; p++)
      inst_flat[p] = imem[cycle_cnt * NPES + p];
  end

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
  logic        buf_page, dma_page;

  // descriptor ring: descriptors live in SoC RAM (32 bytes each)
  logic [31:0] ring_base;
  logic [15:0] ring_size, ring_head, ring_tail, ring_done;
  logic        ring_en, ring_mode, desc_irq, rd_desc, wr_status;
  logic [31:0] desc_addr;
  // RING_TAIL may be programmed as "one past the last entry" == ring_size; that
  // is the same slot as 0, so comparisons must be made modulo the ring depth.
  // Without this, a head that wraps back to 0 never matches a tail of ring_size
  // and the engine re-executes the first descriptors of the batch.
  logic [15:0] ring_tail_n;
  assign ring_tail_n = (ring_size != 16'd0 && ring_tail >= ring_size) ? 16'd0 : ring_tail;

  // descriptor buffer: 4 beats = 32 bytes (the only buffering in the engine -
  // data beats are streamed straight into / out of the scratchpad)
  localparam int DESC_BEATS = 4;
  logic [63:0] desc_buf [0:DESC_BEATS-1];
  logic [1:0]  dma_beat;      // descriptor beat during the fetch
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
  logic [7:0]  dma_nb_raw;
  logic [15:0] dma_wleft, dma_bw, dma_nidx, dma_4k;
  logic [31:0] dma_baddr;
  logic        dma_odd, dma_odd_full, dma_last_odd;
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
    D_BRESP    = 4'd7    // wait for the write response
  } dstate_e;
  dstate_e dstate;
  logic [3:0]  dstate_q;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) dstate_q <= D_IDLE; else dstate_q <= dstate;
  assign dma_wleft     = dma_run_len - dma_idx;
  assign dma_nb_raw    = (dma_wleft >= 16'(2 * DMA_BEATS))
                         ? 8'(DMA_BEATS)
                         : ((dma_wleft + 16'd1) >> 1);
  assign dma_odd_full  = (dma_wleft < 16'(2 * DMA_BEATS)) ? dma_wleft[0] : 1'b0;
  assign dma_baddr     = dma_run_src + {16'b0, dma_idx} * 4;
  assign dma_4k        = (16'd4096 - {4'b0, dma_baddr[11:0]}) >> 3;   // beats to the 4 KiB edge
  assign dma_nb        = (dma_4k >= 16'(DMA_BEATS)) ? dma_nb_raw
                                                    : ((dma_4k >= 16'd1) ? 8'(dma_4k) : 8'd1);
  assign dma_odd       = (dma_nb == dma_nb_raw) ? dma_odd_full : 1'b0;
  assign dma_bw        = {8'b0, dma_nb} * 16'd2 - {15'b0, dma_odd};
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
  assign spad_dma_we0    = (dstate == D_SIN) && m_axi_rvalid && m_axi_rready;
  assign spad_dma_addr0  = dma_cur[9:0];
  assign spad_dma_wdata0 = m_axi_rdata[31:0];
  assign spad_dma_we1    = (dstate == D_SIN) && m_axi_rvalid && m_axi_rready && !dma_last_odd;
  assign spad_dma_addr1  = dma_nxt_cur[9:0];
  assign spad_dma_wdata1 = m_axi_rdata[63:32];
  assign spad_rd_addr0   = dma_cur[9:0];
  assign spad_rd_addr1   = dma_nxt_cur[9:0];

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
    m_axi_araddr  = rd_desc ? (ring_base + {ring_head, 5'b0}) : dma_baddr;
    m_axi_arlen   = rd_desc ? 8'd3 : (dma_nb - 8'd1);
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
    m_axi_wstrb   = (wr_status || dma_last_odd) ? 8'h0F : 8'hFF;
    m_axi_wlast   = wr_status ? 1'b1 : (dma_bcnt == dma_nb - 8'd1);
    m_axi_bready  = (dstate == D_BRESP);
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      dstate      <= D_IDLE;
      dma_busy    <= 1'b0;
      dma_done    <= 1'b0;
      dma_irq_evt <= 1'b0;
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
      dma_run_stride <= 16'd1;
      dma_run_row    <= 16'd0;
      dma_dir     <= 1'b0;
      dma_beat    <= 2'd0;
      dma_bcnt    <= 8'd0;
      dma_page    <= 1'b0;
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
            dma_beat  <= 2'd0;
            dma_bcnt  <= 8'd0;
            ring_mode <= 1'b1;
            rd_desc   <= 1'b1;
            dstate    <= D_AR;
          end
        end
        D_AR: begin
          if (m_axi_arvalid && m_axi_arready) begin
            dma_beat <= 2'd0;
            dma_bcnt <= 8'd0;
            dstate   <= rd_desc ? D_RDESC : D_SIN;
          end
        end
        D_RDESC: begin
          if (m_axi_rvalid && m_axi_rready) begin
            desc_buf[dma_beat] <= m_axi_rdata;
            if (m_axi_rlast) dstate <= D_DESC_SET;
            else             dma_beat <= dma_beat + 2'd1;
          end
        end
        D_DESC_SET: begin
          dma_run_src    <= desc_buf[0][31:0];
          dma_run_len    <= (desc_buf[1][15:0] > 16'd1024) ? 16'd1024 : desc_buf[1][15:0];
          dma_run_stride <= desc_buf[1][47:32];
          dma_run_row    <= desc_buf[1][63:48];
          dma_cur        <= desc_buf[0][47:32];
          dma_page       <= desc_buf[0][48];
          dma_dir        <= desc_buf[0][49];
          desc_irq       <= desc_buf[0][50];
          desc_addr      <= ring_base + {ring_head, 5'b0};
          dma_idx        <= 16'd0;
          dma_row_cnt    <= 16'd0;
          dma_bcnt       <= 8'd0;
          rd_desc        <= 1'b0;
          dstate         <= desc_buf[0][49] ? D_AW : D_AR;
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
              if (dma_nidx >= dma_run_len) begin
                if (ring_mode) begin
                  wr_status <= 1'b1;
                  dstate    <= D_AW;
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
              ring_done <= ring_done + 16'd1;
              ring_head <= (ring_head + 16'd1 >= ring_size) ? 16'd0 : ring_head + 16'd1;
              if (ring_en && (((ring_head + 16'd1 >= ring_size) ? 16'd0
                                                             : ring_head + 16'd1) != ring_tail_n)) begin
                dma_beat <= 2'd0;
                rd_desc  <= 1'b1;
                dstate   <= D_AR;
              end else begin
                dma_busy <= 1'b0;
                dstate   <= D_IDLE;
              end
            end else begin
              dma_idx <= dma_nidx;
              if (dma_nidx >= dma_run_len) begin
                if (ring_mode) begin
                  wr_status <= 1'b1;
                  dstate    <= D_AW;
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
        default: dstate <= D_IDLE;
      endcase
    if (wr_en) begin
      case (wr_addr)
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
            ring_mode  <= 1'b0;
            rd_desc    <= 1'b0;
            wr_status  <= 1'b0;
            // snapshot the descriptor for this transfer
            dma_run_src    <= dma_src;
            dma_run_len    <= dma_len;
            dma_run_stride <= dma_stride;
            dma_run_row    <= dma_row;
            dstate <= wr_data[1] ? D_AW : D_AR;
          end
        end
        default: ;
      endcase
    end
    end
  end

  // ---------------- array sequencer ----------------
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      num_cycles <= 16'd0;
      cycle_cnt  <= 16'd0;
      busy       <= 1'b0;
      done       <= 1'b0;
      buf_page   <= 1'b0;   // single driver: the copy engine never touches it
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
          imem[(wr_addr - REG_IMEM_BASE) >> 2] <= wr_data;
        end
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
      .dma_rd_data1(spad_rd_data1)
  );

  // ---------------- host read mux ----------------
  always_comb begin
    rd_data = 32'b0;
    if (rd_addr < 16'h0064) begin
      case (rd_addr[6:0])
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
        default: rd_data = 32'b0;
      endcase
    end else if (rd_addr >= REG_PE_BASE && rd_addr < (REG_PE_ACC + 16'h40)) begin
      rd_data = pe_rd;
    end else if ((rd_addr >= REG_SPAD_BASE) &&
                 (rd_addr < (REG_SPAD_BASE + SPAD_BYTES))) begin
      rd_data = pe_rd;
    end else if ((rd_addr >= REG_IMEM_BASE) &&
                 (rd_addr < (REG_IMEM_BASE + CGRA_IMEM_WORDS * 4))) begin
      rd_data = imem[(rd_addr - REG_IMEM_BASE) >> 2];
    end
  end

endmodule

`endif
