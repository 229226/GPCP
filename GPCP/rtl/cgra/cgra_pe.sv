// -----------------------------------------------------------------------------
// GPCP CGRA - processing element
//
// 8 x 32-bit registers, one 32-bit accumulator, one 32-bit "link" register.
// The link register holds the value emitted in the previous cycle and is what
// the N/E/S/W neighbours observe, i.e. nearest-neighbour routing with a
// one-cycle latency.
// -----------------------------------------------------------------------------
`ifndef CGRA_PE_SV
`define CGRA_PE_SV

module cgra_pe (
    input  logic        clk,
    input  logic        rst_n,
    input  logic        en,              // execute instruction this cycle
    input  logic [3:0]  op,
    input  logic [3:0]  src_a,
    input  logic [3:0]  src_b,
    input  logic [2:0]  dst,
    input  logic [15:0] imm,
    // neighbour links (previous-cycle values)
    input  logic [31:0] in_n,
    input  logic [31:0] in_e,
    input  logic [31:0] in_s,
    input  logic [31:0] in_w,
    // host debug / preload window
    input  logic        host_we,
    input  logic [3:0]  host_waddr,      // 0..7 = r0..r7, 8 = acc
    input  logic [31:0] host_wdata,
    input  logic [3:0]  host_raddr,      // 0..7 = r0..r7, 8 = acc
    output logic [31:0] host_rdata,
    // scratchpad interface (bank/word address in imm[9:0])
    output logic        spad_rot,        // imm[15]=1：本次 LD/ST 参与循环旋转
    output logic [9:0]  spad_raddr,
    input  logic [31:0] spad_rdata,
    output logic        spad_we,
    output logic [9:0]  spad_waddr,
    output logic [31:0] spad_wdata,
    // emitted value for neighbours
    output logic [31:0] link_out
);

  import cgra_pkg::*;

  logic [31:0] rf [0:7];
  logic [31:0] acc;
  logic [31:0] link;

  logic [31:0] val_a, val_b, prod;
  logic        wr, acc_wr, link_wr;
  logic [31:0] wval, acc_nxt, link_nxt;

  assign spad_raddr = imm[9:0];
  // 只有显式访存指令把 imm[15] 当“参与旋转”标志；其它算子的 imm[15] 仍是立即数符号位
  assign spad_rot   = ((op == OP_LD) || (op == OP_ST)) && imm[15];

  // ---------------- operand selection ----------------
  always_comb begin
    unique case (src_a)
      SRC_R0:  val_a = rf[0];
      SRC_R0 + 4'd1: val_a = rf[1];
      SRC_R0 + 4'd2: val_a = rf[2];
      SRC_R0 + 4'd3: val_a = rf[3];
      SRC_R0 + 4'd4: val_a = rf[4];
      SRC_R0 + 4'd5: val_a = rf[5];
      SRC_R0 + 4'd6: val_a = rf[6];
      SRC_R0 + 4'd7: val_a = rf[7];
      SRC_N:    val_a = in_n;
      SRC_E:    val_a = in_e;
      SRC_S:    val_a = in_s;
      SRC_W:    val_a = in_w;
      SRC_ZERO: val_a = 32'b0;
      SRC_IMM:  val_a = {{16{imm[15]}}, imm};
      SRC_ACC:  val_a = acc;
      SRC_LINK: val_a = link;
      default:  val_a = 32'b0;
    endcase

    unique case (src_b)
      SRC_R0:  val_b = rf[0];
      SRC_R0 + 4'd1: val_b = rf[1];
      SRC_R0 + 4'd2: val_b = rf[2];
      SRC_R0 + 4'd3: val_b = rf[3];
      SRC_R0 + 4'd4: val_b = rf[4];
      SRC_R0 + 4'd5: val_b = rf[5];
      SRC_R0 + 4'd6: val_b = rf[6];
      SRC_R0 + 4'd7: val_b = rf[7];
      SRC_N:    val_b = in_n;
      SRC_E:    val_b = in_e;
      SRC_S:    val_b = in_s;
      SRC_W:    val_b = in_w;
      SRC_ZERO: val_b = 32'b0;
      SRC_IMM:  val_b = {{16{imm[15]}}, imm};
      SRC_ACC:  val_b = acc;
      SRC_LINK: val_b = link;
      default:  val_b = 32'b0;
    endcase
  end

  assign prod = sx16(val_a) * sx16(val_b);

  // ---------------- execution ----------------
  always_comb begin
    wr      = 1'b0;
    acc_wr  = 1'b0;
    link_wr = 1'b0;
    spad_we = 1'b0;
    spad_waddr = 10'b0;
    spad_wdata = 32'b0;
    wval    = 32'b0;
    acc_nxt = acc;
    link_nxt = link;

    unique case (op)
      OP_NOP: ;
      OP_MOV: begin wr = 1'b1; wval = val_a; end
      OP_ADD: begin wr = 1'b1; wval = val_a + val_b; end
      OP_SUB: begin wr = 1'b1; wval = val_a - val_b; end
      OP_MUL: begin wr = 1'b1; wval = prod; end
      OP_SHR: begin wr = 1'b1; wval = $signed(val_a) >>> imm[4:0]; end
      OP_SHL: begin wr = 1'b1; wval = val_a << imm[4:0]; end
      OP_AND: begin wr = 1'b1; wval = val_a & val_b; end
      OP_XOR: begin wr = 1'b1; wval = val_a ^ val_b; end
      OP_MAX: begin wr = 1'b1; wval = ($signed(val_a) > $signed(val_b)) ? val_a : val_b; end
      OP_MIN: begin wr = 1'b1; wval = ($signed(val_a) < $signed(val_b)) ? val_a : val_b; end
      OP_MAC: begin
        // accumulate only: the link keeps the value emitted by the previous
        // register-writing instruction, so a shifted sample stays visible to
        // the neighbours while the MAC is executing
        acc_wr   = 1'b1;
        acc_nxt  = acc + prod;
      end
      OP_MACL: begin
        acc_wr   = 1'b1;
        acc_nxt  = acc + prod;
        link_wr  = 1'b1;
        link_nxt = acc + prod;
      end
      OP_LD: begin
        // load from the distributed scratchpad (combinational read)
        wr   = 1'b1;
        wval = spad_rdata;
      end
      OP_ST: begin
        // store operand A into the scratchpad
        spad_we    = 1'b1;
        spad_waddr = imm[9:0];
        spad_wdata = val_a;
      end
      OP_LACC: begin
        acc_wr   = 1'b1;
        acc_nxt  = val_a;
        link_wr  = 1'b1;
        link_nxt = val_a;
      end
      default: ;
    endcase

    if (wr) begin
      link_wr  = 1'b1;
      link_nxt = wval;
    end
  end

  // ---------------- state ----------------
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      for (int i = 0; i < 8; i++) rf[i] <= 32'b0;
      acc  <= 32'b0;
      link <= 32'b0;
    end else begin
      if (host_we) begin
        if (host_waddr[3]) acc <= host_wdata;
        else               rf[host_waddr[2:0]] <= host_wdata;
      end
      if (en) begin
        if (wr)     rf[dst] <= wval;
        if (acc_wr) acc     <= acc_nxt;
        if (link_wr) link   <= link_nxt;
      end
    end
  end

  assign host_rdata = host_raddr[3] ? acc : rf[host_raddr[2:0]];
  assign link_out   = link;

endmodule

`endif
