// -----------------------------------------------------------------------------
// GPCP CGRA - top level: instruction memory + sequencer + host register file
//
// Host register map (byte offsets, see cgra_pkg):
//   0x0000  W: CTRL {start, clear_done}   R: STATUS {cycle, idle, done, busy}
//   0x0004  W: NUM_CYCLES
//   0x0008  R: CYCLE_COUNT
//   0x000C  R: ID {magic, isa_ver, n, n}
//   0x1000  PE register window  (pe*32 + reg*4), reg 0..7
//   0x1200  PE accumulator window (pe*4)
//   0x2000  instruction memory (word index = cycle*NPES + pe)
// -----------------------------------------------------------------------------
`ifndef CGRA_TOP_SV
`define CGRA_TOP_SV

module cgra_top (
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
    output logic        busy,
    output logic        done,
    output logic [15:0] cycle_o
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
          num_cycles <= (wr_data[15:0] > MAXCYC) ? 16'(MAXCYC) : wr_data[15:0];
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
      end else begin
        // idle: nothing to do
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
      .din    (din)
  );

  // ---------------- host read mux ----------------
  always_comb begin
    rd_data = 32'b0;
    if (rd_addr < 16'h0010) begin
      case (rd_addr[3:0])
        4'h0: rd_data = {cycle_cnt, 13'b0, 1'b0, done, busy};
        4'h4: rd_data = {16'b0, num_cycles};
        4'h8: rd_data = {16'b0, cycle_cnt};
        4'hC: rd_data = {CGRA_MAGIC, CGRA_ISA_VER, 8'(CGRA_N), 8'(CGRA_N)};
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
