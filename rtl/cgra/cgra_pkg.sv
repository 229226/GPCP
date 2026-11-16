// -----------------------------------------------------------------------------
// GPCP CGRA - global definitions (v0.1, M0 prototype)
//
// Time-multiplexed spatial array: all PEs execute the same microcode address
// every cycle, each PE slot in the instruction word is independent (spatial VLIW).
// Data path is int16 (Q15) MAC with 32-bit accumulator.
// -----------------------------------------------------------------------------
`ifndef CGRA_PKG_SV
`define CGRA_PKG_SV

package cgra_pkg;

  // ---------------- array geometry ----------------
  parameter int CGRA_N          = 4;            // N x N PE array
  parameter int CGRA_NPES       = CGRA_N * CGRA_N;
  parameter int CGRA_DW         = 32;           // register / data path width
  parameter int CGRA_IMM_W      = 16;
  parameter int CGRA_IMEM_WORDS = 8192;         // 512 cycles x 16 PEs
  parameter int CGRA_MAX_CYCLES = CGRA_IMEM_WORDS / CGRA_NPES;

  // ---------------- instruction word layout (32 bit) ----------------
  // [15: 0] imm      16-bit immediate / shift amount
  // [18:16] dst      destination register r0..r7
  // [22:19] src_b    operand B selector
  // [26:23] src_a    operand A selector
  // [30:27] op       opcode
  // [31]    rsvd
  parameter int INST_IMM_LSB = 0;
  parameter int INST_DST_LSB = 16;
  parameter int INST_SRCB_LSB = 19;
  parameter int INST_SRCA_LSB = 23;
  parameter int INST_OP_LSB = 27;

  // ---------------- opcodes ----------------
  localparam logic [3:0] OP_NOP  = 4'd0;   // no write, link holds previous value
  localparam logic [3:0] OP_MOV  = 4'd1;   // dst = a
  localparam logic [3:0] OP_ADD  = 4'd2;   // dst = a + b
  localparam logic [3:0] OP_SUB  = 4'd3;   // dst = a - b
  localparam logic [3:0] OP_MUL  = 4'd4;   // dst = sext16(a) * sext16(b)
  localparam logic [3:0] OP_MAC  = 4'd5;   // acc += sext16(a) * sext16(b), link holds
  localparam logic [3:0] OP_LACC = 4'd6;   // acc = a
  localparam logic [3:0] OP_SHR  = 4'd7;   // dst = a >> imm[4:0] (arithmetic)
  localparam logic [3:0] OP_SHL  = 4'd8;   // dst = a << imm[4:0]
  localparam logic [3:0] OP_AND  = 4'd9;   // dst = a & b
  localparam logic [3:0] OP_XOR  = 4'd10;  // dst = a ^ b
  localparam logic [3:0] OP_MAX  = 4'd11;  // dst = max(a, b)   (signed)
  localparam logic [3:0] OP_MIN  = 4'd12;  // dst = min(a, b)   (signed)
  localparam logic [3:0] OP_MACL = 4'd13;  // acc += a*b and emit acc to the link (systolic chain)
  localparam logic [3:0] OP_LD   = 4'd14;  // dst = scratchpad[imm[9:0]]
  localparam logic [3:0] OP_ST   = 4'd15;  // scratchpad[imm[9:0]] = a

  // ---------------- operand selectors ----------------
  // 0..7  : local register file r0..r7
  // 8..11 : neighbour link registers N, E, S, W
  // 12    : zero
  // 13    : instruction immediate (sign extended from 16 bit)
  // 14    : accumulator
  // 15    : own link register (last emitted value)
  localparam logic [3:0] SRC_R0   = 4'd0;
  localparam logic [3:0] SRC_N    = 4'd8;
  localparam logic [3:0] SRC_E    = 4'd9;
  localparam logic [3:0] SRC_S    = 4'd10;
  localparam logic [3:0] SRC_W    = 4'd11;
  localparam logic [3:0] SRC_ZERO = 4'd12;
  localparam logic [3:0] SRC_IMM  = 4'd13;
  localparam logic [3:0] SRC_ACC  = 4'd14;
  localparam logic [3:0] SRC_LINK = 4'd15;

  // dst selector: r0..r7 are 0..7; acc is not a dst (use OP_MAC / OP_LACC)

  // ---------------- host register map (byte addresses) ----------------
  localparam logic [15:0] REG_CTRL       = 16'h0000; // W: [0] start, [1] clear done
  localparam logic [15:0] REG_STATUS     = 16'h0000; // R: [0] busy, [1] done, [2] idle
  localparam logic [15:0] REG_NUM_CYCLES = 16'h0004; // W: [15:0] cycles, [16] buffer page,
                                                     //    [31] start in this write (atomic
                                                     //    "run" register, see docs/02)
  localparam logic [15:0] REG_CYCLE_CNT  = 16'h0008;
  localparam logic [15:0] REG_ID         = 16'h000C; // R: {magic, isa_ver, n, n}

  // ---------------- copy engine (DMA) ----------------
  localparam logic [15:0] REG_DMA_SRC    = 16'h0010; // W: source byte address (SoC RAM)
  localparam logic [15:0] REG_DMA_DST    = 16'h0014; // W: scratchpad index (page-relative, bank*32+word)
  localparam logic [15:0] REG_DMA_LEN    = 16'h0018; // W: number of 32-bit words
  localparam logic [15:0] REG_DMA_CTRL   = 16'h001C; // W: [0]=start [1]=dir(1=spad->mem) [2]=clr-done
                                                     // R: [0]=busy [1]=done
  localparam logic [15:0] REG_BUF_PAGE   = 16'h0020; // array page select (0/1)
  localparam logic [15:0] REG_DMA_PAGE   = 16'h0024; // copy-engine page select (0/1)
  localparam logic [15:0] REG_DMA_STRIDE = 16'h0028; // 2D copy: scratchpad stride between rows
  localparam logic [15:0] REG_DMA_ROW    = 16'h002C; // 2D copy: words per row (0 = linear)

  // ---------------- interrupt (level, sticky until W1C) ----------------
  localparam logic [15:0] REG_IRQ_STATUS = 16'h0040; // R: [0]=array done [1]=dma done
                                                     // W: write 1 to clear
  localparam logic [15:0] REG_IRQ_ENABLE = 16'h0044; // R/W: [0]=array done [1]=dma done
  localparam int IRQ_ARR = 0;
  localparam int IRQ_DMA = 1;

  // ---------------- descriptor ring (descriptors live in SoC RAM) ----------------
  // 32-byte descriptor, must be 8-byte aligned:
  //   +0x00  src (u32, 8-byte aligned)
  //   +0x04  dst spad index (u16) | page<<16 | dir(spad->ram)<<17 |
  //          irq<<18 | last<<19
  //   +0x08  len (u16, words, <= 1024)
  //   +0x0C  stride (u16) | row<<16
  //   +0x10  status written by the engine: 0xC0DE_0000 | len
  //   +0x14 .. +0x1F reserved
  localparam logic [15:0] REG_RING_BASE = 16'h0048; // W/R: byte address of descriptor 0
  localparam logic [15:0] REG_RING_SIZE = 16'h004C; // W/R: ring depth (power of two)
  localparam logic [15:0] REG_RING_HEAD = 16'h0050; // R: next descriptor / W: set
  localparam logic [15:0] REG_RING_TAIL = 16'h0054; // R/W: one past the last valid entry
  localparam logic [15:0] REG_RING_CTRL = 16'h0058; // W: [0] enable
                                                    // R: [0] enabled [1] idle [2] busy
  localparam logic [15:0] REG_RING_DONE = 16'h005C; // R: descriptors completed so far
  localparam logic [15:0] REG_DBG_STATE = 16'h0060; // R: engine FSM state (debug)
  localparam logic [15:0] REG_DESC_MODE = 16'h0064; // W: [0] 1 = 64B 复合描述符
  localparam logic [15:0] REG_WIN_STATUS = 16'h0070; // R: [0] 输出写窗口越界（sticky）
                                                     // W: [0] 写 1 清除

  // ---------------- 每 bank 的循环旋转表（LDPC 的 CN/BN 视图转置） ----------------
  // 32 个条目，每个 7 bit：rot[4:0] / invalid[5] / keep[6]。
  // 只有指令 imm[15]=1 的 LD/ST 参与：addr = (word + rot[bank]) mod ZC。
  //   invalid=1：该 bank 的 LD 返回 0（BN 的缺失边）；
  //   keep=1   ：该 bank 的 ST 被丢弃（CN 的哨兵边，保持 +32767 不被 m' 覆盖）。
  // 表可以逐字写（REG_ROT_BASE），也可以由 ring 的「表装载描述符」
  // （word0[51]=1，32 个 32-bit 条目从 RAM 搬入）批量装载。
  localparam logic [15:0] REG_ROT_ZC   = 16'h006C; // W/R: ZC（0 视为 32）
  localparam logic [15:0] REG_ROT_BASE = 16'h0080; // 0x80 + bank*4
  localparam int          ROT_ENTRIES  = 32;

  // ---------------- performance counters (read-only, 32-bit each) ----------------
  // W: REG_PERF_CTRL[0] = clear all counters (single-cycle pulse).
  // R: REG_PERF_BASE + 4*index, index见 cgra_top 的 perf_counters 映射。
  localparam logic [15:0] REG_PERF_CTRL = 16'h0068;
  localparam logic [15:0] REG_PERF_BASE = 16'h0100;
  localparam int          PERF_N       = 64;

  localparam logic [31:0] DESC_STATUS_MAGIC = 32'hC0DE_0000;

  // ---------------- scratchpad (distributed, banked) ----------------
  // 32 banks x 32 words x 32 bit.  Bank b is normally owned by PE b (data),
  // banks 16..31 are used for per-PE twiddles/coefficients in the kernels.
  // Two pages of 32 banks (double buffering): banks 0..31 = page 0,
  // banks 32..63 = page 1.  LD/ST addresses are page-relative; the active
  // page comes from REG_BUF_PAGE (array) / REG_DMA_PAGE (copy engine).
  parameter int SPAD_BANKS = 64;
  parameter int SPAD_WORDS = 32;
  parameter int SPAD_PAGES = 2;
  parameter int SPAD_BYTES = SPAD_BANKS * SPAD_WORDS * 4;   // 8 KiB
  localparam logic [15:0] REG_SPAD_BASE  = 16'hA000; // bank*128 + word*4

  localparam logic [15:0] REG_PE_BASE    = 16'h1000; // 16 PEs x 8 regs x 4B
  localparam logic [15:0] REG_PE_ACC     = 16'h1200; // 16 PEs x 4B
  localparam logic [15:0] REG_IMEM_BASE  = 16'h2000; // 8192 words x 4B

  localparam logic [7:0]  CGRA_MAGIC     = 8'hCA;
  localparam logic [7:0]  CGRA_ISA_VER   = 8'h01;

  // ---------------- helpers ----------------
  function automatic logic [31:0] sx16(input logic [31:0] v);
    sx16 = {{16{v[15]}}, v[15:0]};
  endfunction

  function automatic logic [31:0] pack_inst(
      input logic [3:0]  op,
      input logic [3:0]  src_a,
      input logic [3:0]  src_b,
      input logic [2:0]  dst,
      input logic [15:0] imm
  );
    pack_inst = {1'b0, op, src_a, src_b, dst, imm};
  endfunction

  // scratchpad address: {bank[4:0], word[4:0]} in the low 10 bits of imm
  function automatic logic [15:0] spad_addr(input logic [4:0] bank, input logic [4:0] word);
    spad_addr = {6'b0, bank, word};
  endfunction

  // ---------------- 拷贝引擎：突发规划（唯一实现） ----------------
  // 一次传输按 AXI 规则切成若干 INCR 突发：每突发 <= DMA_BEATS 拍、不跨
  // 4 KiB 边界、且不超过剩余字数。读通道和写通道都只消费这里的结果，
  // 长度/奇偶/裁剪不再各算一遍（2026-11-15 的越界 bug 就出在裁剪那一步）。
  typedef struct packed {
    logic [7:0]  nb;    // 本突发 beat 数（1..DMA_BEATS）
    logic        odd;   // 最后一拍只有低 32 bit（字数为奇数）
    logic [15:0] bw;    // 本突发的字数 = 2*nb - odd
  } dma_burst_t;

  function automatic dma_burst_t dma_plan_burst(
      input logic [15:0] wleft,   // 剩余字数
      input logic [11:0] lo,      // 当前地址低 12 位
      input int unsigned beats);  // DMA_BEATS
    logic [15:0] to4k, words;
    dma_burst_t p;
    to4k  = (16'd4096 - {4'b0, lo}) >> 3;                 // 到 4 KiB 边界的拍数
    words = (wleft >= 16'(2 * beats)) ? 16'(2 * beats) : wleft;
    p.nb  = 8'((words + 16'd1) >> 1);
    p.odd = (wleft < 16'(2 * beats)) ? wleft[0] : 1'b0;
    if ({8'b0, p.nb} > to4k) begin                        // 4 KiB 裁剪（取 min）
      p.nb  = 8'(to4k);
      p.odd = 1'b0;
    end
    p.bw = {8'b0, p.nb} * 16'd2 - {15'b0, p.odd};
    dma_plan_burst = p;
  endfunction

endpackage

`endif
