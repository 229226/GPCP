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
  localparam logic [15:0] REG_NUM_CYCLES = 16'h0004;
  localparam logic [15:0] REG_CYCLE_CNT  = 16'h0008;
  localparam logic [15:0] REG_ID         = 16'h000C; // R: {magic, isa_ver, n, n}

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

endpackage

`endif
