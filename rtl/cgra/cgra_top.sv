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
    parameter bit DBG = 1'b0
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
    // copy-engine master port (one 32-bit word per handshake)
    output logic        dma_valid,
    output logic        dma_we,
    output logic [31:0] dma_addr,
    output logic [31:0] dma_wdata,
    input  logic [31:0] dma_rdata,
    input  logic        dma_ready,
    output logic        busy,
    output logic        done,
    output logic [15:0] cycle_o,
    // level interrupt: array done and/or copy-engine done (masked by ENABLE)
    output logic        irq_o
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
  // a transfer starts, so software may already program the next descriptor
  // while the current copy is in flight (ping-pong scheduling).
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
  logic [15:0] dma_idx;
  logic        dma_busy, dma_done, dma_dir;
  logic [31:0] dma_rdata_q;
  logic        buf_page, dma_page;

  // ---------------- interrupt status / enable ----------------
  // sticky status bits, cleared by writing 1 to REG_IRQ_STATUS; the line is
  // level sensitive so the PLIC sees it until software acknowledges
  logic [1:0]  irq_status, irq_enable;
  logic        done_q, dma_done_q;
  logic [1:0]  irq_evts;

  assign irq_o  = |(irq_status & irq_enable);
  assign irq_evts = {dma_done & ~dma_done_q, done & ~done_q};

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

  typedef enum logic [2:0] {
    D_IDLE   = 3'd0,
    D_MEM_RD = 3'd1,
    D_SPAD_W = 3'd2,
    D_SPAD_R = 3'd3,
    D_MEM_WR = 3'd4
  } dstate_e;
  dstate_e dstate;

  // scratchpad side of the copy engine
  logic        spad_dma_we;
  logic [9:0]  spad_dma_addr;
  logic [31:0] spad_dma_wdata;
  logic [9:0]  dma_rd_idx;
  logic [31:0] dma_rd_data;

  always_comb begin
    dma_valid     = 1'b0;
    dma_we        = 1'b0;
    dma_addr      = dma_run_src + {16'b0, dma_idx} * 4;
    dma_wdata     = 32'b0;
    spad_dma_we   = 1'b0;
    spad_dma_addr = dma_cur[9:0];
    spad_dma_wdata = dma_rdata_q;
    dma_rd_idx    = dma_cur[9:0];
    unique case (dstate)
      D_MEM_RD: dma_valid = 1'b1;
      D_SPAD_W: spad_dma_we = 1'b1;
      D_MEM_WR: begin
        dma_valid = 1'b1;
        dma_we    = 1'b1;
        dma_wdata = dma_rd_data;
      end
      default: ;
    endcase
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      dstate      <= D_IDLE;
      dma_busy    <= 1'b0;
      dma_done    <= 1'b0;
      dma_idx     <= 16'd0;
      dma_rdata_q <= 32'b0;
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
      buf_page    <= 1'b0;
      dma_page    <= 1'b0;
    end else begin
      if (wr_en) begin
        case (wr_addr)
          REG_DMA_SRC:  dma_src  <= wr_data;
          REG_DMA_DST:  dma_dst  <= wr_data[15:0];
          REG_DMA_LEN:  dma_len  <= (wr_data[15:0] > 16'd1024) ? 16'd1024 : wr_data[15:0];
          REG_DMA_CTRL: begin
            if (wr_data[2]) dma_done <= 1'b0;
            if (wr_data[0] && !dma_busy && (dma_len != 16'd0)) begin
              dma_busy <= 1'b1;
              dma_done <= 1'b0;
              dma_idx  <= 16'd0;
              dma_cur  <= dma_dst;
              dma_row_cnt <= 16'd0;
              dma_dir  <= wr_data[1];
              dstate   <= wr_data[1] ? D_SPAD_R : D_MEM_RD;
              // snapshot the descriptor for this transfer
              dma_run_src    <= dma_src;
              dma_run_len    <= dma_len;
              dma_run_stride <= dma_stride;
              dma_run_row    <= dma_row;
            end
          end
          REG_BUF_PAGE: ;   // handled by the sequencer block (single driver)
          REG_DMA_PAGE: dma_page <= wr_data[0];
          REG_DMA_STRIDE: dma_stride <= wr_data[15:0];
          REG_DMA_ROW:    dma_row    <= wr_data[15:0];
          default: ;
        endcase
      end else begin
        unique case (dstate)
          D_IDLE: ;
          D_MEM_RD: begin
            if (dma_ready) begin
              dma_rdata_q <= dma_rdata;
              dstate      <= D_SPAD_W;
            end
          end
          D_SPAD_W: begin
            dstate <= D_MEM_RD;
            if (dma_idx == dma_run_len - 16'd1) begin
              dma_busy <= 1'b0;
              dma_done <= 1'b1;
              dstate   <= D_IDLE;
            end else begin
              dma_idx <= dma_idx + 16'd1;
              {dma_cur, dma_row_cnt} <= dma_next(dma_cur, dma_row_cnt, dma_run_row, dma_run_stride);
            end
          end
          D_SPAD_R: dstate <= D_MEM_WR;
          D_MEM_WR: begin
            if (dma_ready) begin
              if (dma_idx == dma_run_len - 16'd1) begin
                dma_busy <= 1'b0;
                dma_done <= 1'b1;
                dstate   <= D_IDLE;
              end else begin
                dma_idx <= dma_idx + 16'd1;
                {dma_cur, dma_row_cnt} <= dma_next(dma_cur, dma_row_cnt, dma_run_row, dma_run_stride);
                dstate  <= D_SPAD_R;
              end
            end
          end
          default: dstate <= D_IDLE;
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
      .dma_we   (spad_dma_we),
      .dma_addr (spad_dma_addr),
      .dma_wdata(spad_dma_wdata),
      .dma_rd_addr(dma_rd_idx),
      .dma_rd_data(dma_rd_data)
  );

  // ---------------- host read mux ----------------
  always_comb begin
    rd_data = 32'b0;
    if (rd_addr < 16'h0048) begin
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
