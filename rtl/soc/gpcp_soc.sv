// -----------------------------------------------------------------------------
// GPCP SoC (milestone B2)
//
//   CVA6 (RV64GC, WT cache, 64-bit AXI4 master)
//      |
//      +-- 0x8000_0000 .. 0x8007_FFFF  : 512 KiB RAM (cacheable, bursts)
//      +-- 0x4000_0000 .. 0x4000_FFFF  : GPCP CGRA (uncached, 32-bit registers)
//      +-- 0x4001_xxxx                 : sim control (write = exit)
//
// The AXI slave logic supports a single outstanding transaction (the master
// is throttled through arready/awready), which is legal AXI and sufficient
// for both the cache-miss engine and uncached MMIO.
// -----------------------------------------------------------------------------
`ifndef GPCP_SOC_SV
`define GPCP_SOC_SV

module gpcp_soc (
    input  logic        clk_i,
    input  logic        rst_ni,
    // backdoor memory access for the Verilator testbench (word granularity)
    input  logic        ram_we,
    input  logic [15:0] ram_waddr,
    input  logic [63:0] ram_wdata,
    input  logic [15:0] ram_raddr,
    output logic [63:0] ram_rdata,
    // simulation control
    output logic        exit_o,
    output logic [31:0] exit_code_o
);

  import ariane_pkg::*;
  import cgra_pkg::*;

  localparam config_pkg::cva6_cfg_t CVA6Cfg =
      build_config_pkg::build_config(cva6_config_pkg::cva6_cfg);

  localparam type axi_req_t = ariane_axi::req_t;
  localparam type axi_rsp_t = ariane_axi::resp_t;

  // ---------------------------------------------------------------- core
  axi_req_t core_req;
  axi_rsp_t core_rsp;

  ariane #(
      .CVA6Cfg (CVA6Cfg)
  ) u_core (
      .clk_i       (clk_i),
      .rst_ni      (rst_ni),
      .boot_addr_i (64'h8000_0000),
      .hart_id_i   (64'h0),
      .irq_i       (2'b0),
      .ipi_i       (1'b0),
      .time_irq_i  (1'b0),
      .debug_req_i (1'b0),
      .rvfi_probes_o(),
      .noc_req_o   (core_req),
      .noc_resp_i  (core_rsp)
  );

  // ---------------------------------------------------------------- memory
  localparam int RAM_WORDS = 1 << 16;   // 64K x 64 bit = 512 KiB
  logic [63:0] ram [0:RAM_WORDS-1];

  function automatic logic [7:0] ram_byte(input logic [18:0] a);
    ram_byte = ram[a[18:3]][{a[2:0], 3'b000} +: 8];
  endfunction

  // ---------------------------------------------------------------- CGRA
  logic        cgra_busy, cgra_done;
  logic [15:0] cgra_cycle;
  logic [31:0] cgra_rd;
  logic        cgra_wr_en;
  logic [15:0] cgra_wr_addr;
  logic [31:0] cgra_wr_data;
  logic [15:0] cgra_rd_addr;

  cgra_top u_cgra (
      .clk    (clk_i),
      .rst_n  (rst_ni),
      .wr_en  (cgra_wr_en),
      .wr_addr(cgra_wr_addr),
      .wr_data(cgra_wr_data),
      .rd_addr(cgra_rd_addr),
      .rd_data(cgra_rd),
      .din    (32'b0),
      .busy   (cgra_busy),
      .done   (cgra_done),
      .cycle_o(cgra_cycle)
  );

  // ---------------------------------------------------------------- AXI slave FSM
  typedef enum logic [1:0] {
    S_IDLE = 2'd0,
    S_RD   = 2'd1,
    S_WR   = 2'd2,
    S_B    = 2'd3
  } state_e;

  state_e state;

  typedef enum logic [1:0] {R_RAM = 2'd0, R_CGRA = 2'd1, R_SIM = 2'd2, R_ERR = 2'd3} region_e;

  logic [63:0] rd_addr_q, wr_addr_q;
  logic [7:0]  rd_len_q, wr_len_q, rd_cnt_q, wr_cnt_q;
  logic [2:0]  rd_size_q, wr_size_q;
  logic [3:0]  rd_id_q, wr_id_q;
  logic [1:0]  rd_region_q, wr_region_q;
  logic [7:0]  rd_bpb, wr_bpb;

  function automatic region_e decode_region(input logic [63:0] addr);
    if (addr[63:32] != 32'h0)
      decode_region = R_ERR;
    else if (addr[31:16] == 16'h8000) decode_region = R_RAM;   // 512 KiB window
    else if (addr[31:16] == 16'h4000) decode_region = R_CGRA;
    else if (addr[31:16] == 16'h4001) decode_region = R_SIM;
    else decode_region = R_ERR;
  endfunction

  logic aw_accept, ar_accept;
  assign aw_accept = (state == S_IDLE) & core_req.aw_valid & core_req.w_valid & ~core_req.ar_valid;
  assign ar_accept = (state == S_IDLE) & core_req.ar_valid;

  assign rd_bpb = 8'b1 << rd_size_q;
  assign wr_bpb = 8'b1 << wr_size_q;

  // ---------------------------------------------------------------- read data
  logic [63:0] rd_data_c;
  logic [4:0]  rd_off, rd_end;
  assign rd_off = {2'b0, rd_addr_q[2:0]};
  assign rd_end = rd_off + {2'b0, rd_bpb};

  always_comb begin
    rd_data_c = 64'b0;
    if (rd_region_q == R_RAM) begin
      // AXI byte lanes follow the addressed bytes, not lane 0
      for (int b = 0; b < 8; b++) begin
        if ((5'(b) >= rd_off) && (5'(b) < rd_end))
          rd_data_c[b*8 +: 8] = ram_byte({rd_addr_q[18:3], 3'b000} + 19'(b));
      end
    end else if (rd_region_q == R_CGRA) begin
      // the CGRA registers are 32 bit; place them on the addressed byte lanes
      if (rd_addr_q[2:0] == 3'd0) rd_data_c[31:0] = cgra_rd;
      else                        rd_data_c[63:32] = cgra_rd;
    end
  end

  assign cgra_rd_addr = rd_addr_q[15:0];

  // ---------------------------------------------------------------- CGRA write port
  always_comb begin
    cgra_wr_en   = 1'b0;
    cgra_wr_addr = core_req.aw.addr[15:0];
    cgra_wr_data = (core_req.aw.addr[2:0] == 3'd0) ? core_req.w.data[31:0]
                                                  : core_req.w.data[63:32];
    if (aw_accept && (decode_region(core_req.aw.addr) == R_CGRA))
      cgra_wr_en = 1'b1;
  end

  // ---------------------------------------------------------------- exit
  logic        exit_q;
  logic [63:0] exit_code_q;
  assign exit_o      = exit_q;
  assign exit_code_o = exit_code_q[31:0];

  // ---------------------------------------------------------------- FSM + RAM writes
  always_ff @(posedge clk_i or negedge rst_ni) begin
    // backdoor preload works even while the reset is asserted
    if (ram_we) ram[ram_waddr] <= ram_wdata;

    if (!rst_ni) begin
      state       <= S_IDLE;
      rd_addr_q   <= 64'b0;
      wr_addr_q   <= 64'b0;
      rd_len_q    <= 8'b0;
      wr_len_q    <= 8'b0;
      rd_cnt_q    <= 8'b0;
      wr_cnt_q    <= 8'b0;
      rd_size_q   <= 3'b0;
      wr_size_q   <= 3'b0;
      rd_id_q     <= 4'b0;
      wr_id_q     <= 4'b0;
      rd_region_q <= R_ERR;
      wr_region_q <= R_ERR;
      exit_q      <= 1'b0;
      exit_code_q <= 64'b0;
    end else begin
      unique case (state)
        S_IDLE: begin
          if (ar_accept) begin
            rd_addr_q   <= core_req.ar.addr;
            rd_len_q    <= core_req.ar.len;
            rd_size_q   <= core_req.ar.size;
            rd_id_q     <= core_req.ar.id;
            rd_cnt_q    <= 8'b0;
            rd_region_q <= decode_region(core_req.ar.addr);
            state       <= S_RD;
          end else if (aw_accept) begin
            wr_addr_q   <= core_req.aw.addr;
            wr_len_q    <= core_req.aw.len;
            wr_size_q   <= core_req.aw.size;
            wr_id_q     <= core_req.aw.id;
            wr_cnt_q    <= 8'b0;
            wr_region_q <= decode_region(core_req.aw.addr);

            if (decode_region(core_req.aw.addr) == R_RAM) begin
              for (int b = 0; b < 8; b++) begin
                if (core_req.w.strb[b])
                  ram[core_req.aw.addr[18:3]][b*8 +: 8] <= core_req.w.data[b*8 +: 8];
              end
            end else if (decode_region(core_req.aw.addr) == R_SIM) begin
              exit_q      <= 1'b1;
              exit_code_q <= {32'b0, core_req.w.data[31:0]};
            end

            if (core_req.w.last) state <= S_B;
            else                 state <= S_WR;
          end
        end

        S_RD: begin
          if (core_req.r_ready) begin
            if (rd_cnt_q == rd_len_q) begin
              state <= S_IDLE;
            end else begin
              rd_addr_q <= rd_addr_q + {56'b0, rd_bpb};
              rd_cnt_q  <= rd_cnt_q + 8'd1;
            end
          end
        end

        S_WR: begin
          if (core_req.w_valid) begin
            if (wr_region_q == R_RAM) begin
              for (int b = 0; b < 8; b++) begin
                if (core_req.w.strb[b])
                  ram[wr_addr_q[18:3]][b*8 +: 8] <= core_req.w.data[b*8 +: 8];
              end
            end else if (wr_region_q == R_SIM) begin
              exit_q      <= 1'b1;
              exit_code_q <= {32'b0, core_req.w.data[31:0]};
            end

            if (core_req.w.last) begin
              state <= S_B;
            end else begin
              wr_addr_q <= wr_addr_q + {56'b0, wr_bpb};
              wr_cnt_q  <= wr_cnt_q + 8'd1;
            end
          end
        end

        S_B: begin
          if (core_req.b_ready) state <= S_IDLE;
        end

        default: state <= S_IDLE;
      endcase
    end
  end

  // ---------------------------------------------------------------- responses
  always_comb begin
    core_rsp           = '0;
    core_rsp.aw_ready  = aw_accept;
    core_rsp.ar_ready  = ar_accept;
    core_rsp.w_ready   = (state == S_WR) || (state == S_IDLE && aw_accept);
    core_rsp.b_valid   = (state == S_B);
    core_rsp.b.id      = wr_id_q;
    core_rsp.b.resp    = axi_pkg::RESP_OKAY;
    core_rsp.r_valid   = (state == S_RD);
    core_rsp.r.data  = rd_data_c;
    core_rsp.r.id    = rd_id_q;
    core_rsp.r.resp  = (rd_region_q == R_ERR) ? axi_pkg::RESP_SLVERR : axi_pkg::RESP_OKAY;
    core_rsp.r.last  = (rd_cnt_q == rd_len_q);
  end

  assign ram_rdata = ram[ram_raddr];

endmodule

`endif
