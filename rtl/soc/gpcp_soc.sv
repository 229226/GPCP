// -----------------------------------------------------------------------------
// GPCP SoC (milestone B3: Linux-capable)
//
// Memory map follows the QEMU "virt" layout so that stock RISC-V Linux and
// OpenSBI drivers work unmodified:
//
//   0x0200_0000  CLINT   (msip / mtimecmp / mtime)
//   0x0C00_0000  PLIC    (minimal register file, no external sources)
//   0x1000_0000  UART0   (16550A, TX only -> $write)
//   0x4001_0000  SIM     (write = exit)
//   0x5000_0000  CGRA    (GPCP CGRA register window, 64 KiB)
//   0x8000_0000  64 MiB  RAM (cacheable)
//
// AXI slave: single outstanding transaction; bursts supported for the cache
// line refill engine; byte lanes handled per the AXI spec.
// -----------------------------------------------------------------------------
`ifndef GPCP_SOC_SV
`define GPCP_SOC_SV

`include "rvfi_types.svh"

module gpcp_soc #(
    // set to 1 (Verilator -GGPCP_DBG=1) to print PC / AXI / atomic traces
    parameter bit GPCP_DBG = 1'b0
) (
    input  logic        clk_i,
    input  logic        rst_ni,
    // backdoor memory access for the Verilator testbench (64-bit word index)
    input  logic        ram_we,
    input  logic [22:0] ram_waddr,
    input  logic [63:0] ram_wdata,
    input  logic [22:0] ram_raddr,
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

  // RVFI probes are used only for the debug PC trace
  localparam type rvfi_instr_t = `RVFI_PROBES_INSTR_T(CVA6Cfg);
  typedef struct packed {
    logic      csr;
    rvfi_instr_t instr;
  } rvfi_probes_t;

  // ---------------------------------------------------------------- peripherals
  logic [63:0] mtime;
  logic [63:0] mtimecmp;
  logic [31:0] msip;
  logic        time_irq;
  logic        plic_irq;

  assign time_irq = (mtime >= mtimecmp);
  assign plic_irq = 1'b0;

  // UART register file (16550A subset)
  logic [7:0] uart_ier, uart_lcr, uart_mcr, uart_scr;
  logic [7:0] uart_dll, uart_dlm;

  // ---------------------------------------------------------------- core
  axi_req_t core_req;
  axi_rsp_t core_rsp;

  rvfi_probes_t rvfi_probes;

  ariane #(
      .CVA6Cfg          (CVA6Cfg),
      .rvfi_probes_instr_t (rvfi_instr_t),
      .rvfi_probes_t    (rvfi_probes_t)
  ) u_core (
      .clk_i       (clk_i),
      .rst_ni      (rst_ni),
      .boot_addr_i (64'h8000_0000),
      .hart_id_i   (64'h0),
      .irq_i       ({plic_irq, msip[0]}),
      .ipi_i       (1'b0),
      .time_irq_i  (time_irq),
      .debug_req_i (1'b0),
      .rvfi_probes_o (rvfi_probes),
      .noc_req_o   (core_req),
      .noc_resp_i  (core_rsp)
  );

  // ---------------------------------------------------------------- memory
  localparam int RAM_BYTES  = 64 * 1024 * 1024;
  localparam int RAM_WORDS  = RAM_BYTES / 8;   // 8 Mi words

  logic [63:0] ram [0:RAM_WORDS-1];

  function automatic logic [7:0] ram_byte(input logic [25:0] a);
    ram_byte = ram[a[25:3]][{a[2:0], 3'b000} +: 8];
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
  typedef enum logic [2:0] {
    S_IDLE = 3'd0,
    S_RD   = 3'd1,
    S_WR   = 3'd2,
    S_B    = 3'd3,
    S_ATOM_B = 3'd4,
    S_ATOM_R = 3'd5
  } state_e;

  typedef enum logic [2:0] {
    R_RAM  = 3'd0,
    R_CLINT= 3'd1,
    R_PLIC = 3'd2,
    R_UART = 3'd3,
    R_CGRA = 3'd4,
    R_SIM  = 3'd5,
    R_ERR  = 3'd6
  } region_e;

  state_e  state;
  region_e rd_region_q, wr_region_q;

  logic [63:0] rd_addr_q, wr_addr_q;
  logic [7:0]  rd_len_q, wr_len_q, rd_cnt_q, wr_cnt_q;
  logic [2:0]  rd_size_q, wr_size_q;
  logic [3:0]  rd_id_q, wr_id_q;
  logic [7:0]  rd_bpb, wr_bpb;
  // AXI exclusive access (LR/SC) reservation
  logic        resv_valid;
  logic [63:0] resv_addr_q;
  logic [1:0]  bresp_q;
  logic        rd_excl_q;
  logic [63:0] atom_old_q;
  logic        atom_b_done, atom_r_done;
  logic [7:0]  atom_dbg_cnt;

  function automatic region_e decode_region(input logic [63:0] addr);
    if (addr[63:32] != 32'h0)                    decode_region = R_ERR;
    else if (addr[31:26] == 6'b100000)           decode_region = R_RAM;   // 64 MiB
    else if (addr[31:16] == 16'h0200)            decode_region = R_CLINT;
    else if (addr[31:20] == 12'h0C0)             decode_region = R_PLIC;
    else if (addr[31:16] == 16'h1000)            decode_region = R_UART;
    else if (addr[31:16] == 16'h4001)            decode_region = R_SIM;
    else if (addr[31:16] == 16'h5000)            decode_region = R_CGRA;
    else                                         decode_region = R_ERR;
  endfunction

  logic aw_accept, ar_accept;
  assign aw_accept = (state == S_IDLE) & core_req.aw_valid & core_req.w_valid & ~core_req.ar_valid;
  assign ar_accept = (state == S_IDLE) & core_req.ar_valid;

  assign rd_bpb = 8'b1 << rd_size_q;

  // ---------------------------------------------------------------- peripheral read helpers
  function automatic logic [63:0] clint_read(input logic [15:0] off);
    case (off)
      16'h0000: clint_read = {32'b0, msip};
      16'h4000: clint_read = mtimecmp;
      16'hBFF8: clint_read = mtime;
      default:  clint_read = 64'b0;
    endcase
  endfunction

  function automatic logic [31:0] uart_read(input logic [15:0] off);
    // 16550 with reg-shift = 2: register index = byte offset [4:2]
    case (off[4:2])
      3'd0: uart_read = uart_lcr[7] ? {24'b0, uart_dll} : 32'b0;   // RBR (no rx)
      3'd1: uart_read = uart_lcr[7] ? {24'b0, uart_dlm} : {24'b0, uart_ier};
      3'd2: uart_read = 32'h0000_0001;                             // IIR: no pending irq
      3'd3: uart_read = {24'b0, uart_lcr};
      3'd4: uart_read = {24'b0, uart_mcr};
      3'd5: uart_read = 32'h0000_0060;                             // LSR: THRE | TEMT
      3'd6: uart_read = 32'h0000_0020;                             // MSR
      3'd7: uart_read = {24'b0, uart_scr};
      default: uart_read = 32'b0;
    endcase
  endfunction

  // ---------------------------------------------------------------- read data
  logic [63:0] rd_data_c;
  logic [2:0]  rd_off;
  logic [63:0] periph64;
  logic [31:0] periph32;
  logic [4:0]  rd_start, rd_stop, lane;

  assign rd_off   = rd_addr_q[2:0];
  assign rd_start = {2'b0, rd_off};
  assign rd_stop  = rd_start + {2'b0, rd_bpb};

  always_comb begin
    rd_data_c = 64'b0;
    periph64  = 64'b0;
    periph32  = 32'b0;
    case (rd_region_q)
      R_RAM: begin
        for (int b = 0; b < 8; b++) begin
          lane = b[4:0];
          if ((lane >= rd_start) && (lane < rd_stop))
            rd_data_c[b*8 +: 8] = ram_byte(rd_addr_q[25:0] + 26'(b));
        end
      end
      // 32-bit peripherals: replicate on both halves so that a 4-byte access
      // at offset 4 still returns the register value on the addressed lanes
      R_CLINT: periph64 = clint_read(rd_addr_q[15:0]);
      R_PLIC:  periph64 = 64'b0;
      R_UART:  begin
        periph32 = uart_read(rd_addr_q[15:0]);
        periph64 = {periph32, periph32};
      end
      R_CGRA:  periph64 = {cgra_rd, cgra_rd};
      default: periph64 = 64'b0;
    endcase

    if (rd_region_q != R_RAM) begin
      for (int b = 0; b < 8; b++) begin
        lane = b[4:0]; if ((lane >= rd_start) && (lane < rd_stop))
          rd_data_c[b*8 +: 8] = periph64[b*8 +: 8];
      end
    end
  end

  assign cgra_rd_addr = rd_addr_q[15:0];

  // ---------------------------------------------------------------- CGRA write port
  logic [31:0] cgra_wdata_lane;
  assign cgra_wdata_lane = (core_req.aw.addr[2:0] == 3'd0) ? core_req.w.data[31:0]
                                                           : core_req.w.data[63:32];
  always_comb begin
    cgra_wr_en   = 1'b0;
    cgra_wr_addr = core_req.aw.addr[15:0];
    cgra_wr_data = cgra_wdata_lane;
    if (aw_accept && (decode_region(core_req.aw.addr) == R_CGRA))
      cgra_wr_en = 1'b1;
  end

  // ---------------------------------------------------------------- exit
  logic        exit_q;
  logic [63:0] exit_code_q;
  assign exit_o      = exit_q;
  assign exit_code_o = exit_code_q[31:0];

  // ---------------------------------------------------------------- timer
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      mtime    <= 64'b0;
      mtimecmp <= 64'hFFFF_FFFF_FFFF_FFFF;
      msip     <= 32'b0;
    end else begin
      mtime <= mtime + 64'd1;
    end
  end

  // ---------------------------------------------------------------- FSM + writes
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
      atom_old_q  <= 64'b0;
      atom_b_done <= 1'b0;
      atom_r_done <= 1'b0;
      resv_valid  <= 1'b0;
      resv_addr_q <= 64'b0;
      bresp_q     <= axi_pkg::RESP_OKAY;
      rd_excl_q   <= 1'b0;
      uart_ier    <= 8'b0;
      uart_lcr    <= 8'b0;
      uart_mcr    <= 8'b0;
      uart_scr    <= 8'b0;
      uart_dll    <= 8'b0;
      uart_dlm    <= 8'b0;
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
            rd_excl_q   <= core_req.ar.lock;
            if (core_req.ar.lock) begin
              resv_valid  <= 1'b1;
              resv_addr_q <= {core_req.ar.addr[63:3], 3'b000};
            end
            state       <= S_RD;
          end else if (aw_accept && (core_req.aw.atop != 0) &&
                       (decode_region(core_req.aw.addr) == R_RAM)) begin
            // AXI atomic: read-modify-write, return the old value on RVALID
            automatic logic [63:0] old_w;
            automatic logic [63:0] new_w;
            automatic logic [2:0]  lane0;
            automatic int          nbytes;
            old_w  = ram[core_req.aw.addr[25:3]];
            new_w  = old_w;
            lane0  = core_req.aw.addr[2:0];
            nbytes = 1 << core_req.aw.size;
            for (int b = 0; b < 8; b++) begin
              if ((b >= lane0) && (b < lane0 + nbytes)) begin
                case (core_req.aw.atop[5:4])
                  2'b11: new_w[b*8 +: 8] = core_req.w.data[b*8 +: 8];        // swap
                  2'b01: new_w[b*8 +: 8] = old_w[b*8 +: 8] + core_req.w.data[b*8 +: 8]; // add (bytewise fallback)
                  default: new_w[b*8 +: 8] = core_req.w.data[b*8 +: 8];      // treat as swap
                endcase
              end
            end
            if (GPCP_DBG)
              $display("[atom] addr=%h id=%b atop=%b size=%0d wdata=%h old=%h",
                       core_req.aw.addr, core_req.aw.id, core_req.aw.atop,
                       core_req.aw.size, core_req.w.data, old_w);
            ram[core_req.aw.addr[25:3]] <= new_w;
            atom_old_q  <= old_w;
            wr_id_q     <= core_req.aw.id;
            if (core_req.aw.lock) begin
              if (resv_valid && (core_req.aw.addr[63:3] == resv_addr_q[63:3])) begin
                bresp_q    <= axi_pkg::RESP_EXOKAY;
              end else begin
                bresp_q    <= axi_pkg::RESP_OKAY;
              end
              resv_valid <= 1'b0;
            end else begin
              bresp_q <= axi_pkg::RESP_OKAY;
              if (resv_valid && (core_req.aw.addr[63:3] == resv_addr_q[63:3]))
                resv_valid <= 1'b0;
            end
            state       <= S_ATOM_R;
          end else if (aw_accept) begin
            wr_addr_q   <= core_req.aw.addr;
            wr_len_q    <= core_req.aw.len;
            wr_size_q   <= core_req.aw.size;
            wr_id_q     <= core_req.aw.id;
            wr_cnt_q    <= 8'b0;
            wr_region_q <= decode_region(core_req.aw.addr);

            if (core_req.aw.lock) begin
              if (resv_valid && (core_req.aw.addr[63:3] == resv_addr_q[63:3])) begin
                bresp_q    <= axi_pkg::RESP_EXOKAY;
              end else begin
                bresp_q    <= axi_pkg::RESP_OKAY;
              end
              resv_valid <= 1'b0;
            end else begin
              bresp_q <= axi_pkg::RESP_OKAY;
              if (resv_valid && (core_req.aw.addr[63:3] == resv_addr_q[63:3]))
                resv_valid <= 1'b0;
            end

            case (decode_region(core_req.aw.addr))
              R_RAM: begin
                for (int b = 0; b < 8; b++) begin
                  if (core_req.w.strb[b])
                    ram[core_req.aw.addr[25:3]][b*8 +: 8] <= core_req.w.data[b*8 +: 8];
                end
              end
              R_CLINT: begin
                case (core_req.aw.addr[15:0])
                  16'h0000: if (core_req.w.strb[0]) msip <= core_req.w.data[31:0];
                  16'h4000: begin
                    if (core_req.w.strb[0]) mtimecmp[7:0]   <= core_req.w.data[7:0];
                    if (core_req.w.strb[1]) mtimecmp[15:8]  <= core_req.w.data[15:8];
                    if (core_req.w.strb[2]) mtimecmp[23:16] <= core_req.w.data[23:16];
                    if (core_req.w.strb[3]) mtimecmp[31:24] <= core_req.w.data[31:24];
                    if (core_req.w.strb[4]) mtimecmp[39:32] <= core_req.w.data[39:32];
                    if (core_req.w.strb[5]) mtimecmp[47:40] <= core_req.w.data[47:40];
                    if (core_req.w.strb[6]) mtimecmp[55:48] <= core_req.w.data[55:48];
                    if (core_req.w.strb[7]) mtimecmp[63:56] <= core_req.w.data[63:56];
                  end
                  16'h4004: begin
                    if (core_req.w.strb[0]) mtimecmp[39:32] <= core_req.w.data[7:0];
                    if (core_req.w.strb[1]) mtimecmp[47:40] <= core_req.w.data[15:8];
                    if (core_req.w.strb[2]) mtimecmp[55:48] <= core_req.w.data[23:16];
                    if (core_req.w.strb[3]) mtimecmp[63:56] <= core_req.w.data[31:24];
                  end
                  default: ;
                endcase
              end
              R_UART: begin
                case (core_req.aw.addr[4:2])
                  3'd0: begin
                    if (core_req.w.strb[0]) begin
                      $write("%c", core_req.w.data[7:0]);
                      $fflush();
                    end
                  end
                  3'd1: if (core_req.w.strb[0]) uart_ier <= core_req.w.data[7:0];
                  3'd3: if (core_req.w.strb[0]) uart_lcr <= core_req.w.data[7:0];
                  3'd4: if (core_req.w.strb[0]) uart_mcr <= core_req.w.data[7:0];
                  3'd7: if (core_req.w.strb[0]) uart_scr <= core_req.w.data[7:0];
                  default: ;
                endcase
              end
              R_SIM: begin
                exit_q      <= 1'b1;
                exit_code_q <= {32'b0, core_req.w.data[31:0]};
              end
              default: ;
            endcase

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
                  ram[wr_addr_q[25:3]][b*8 +: 8] <= core_req.w.data[b*8 +: 8];
              end
            end else if (wr_region_q == R_UART) begin
              if (wr_addr_q[4:2] == 3'd0 && core_req.w.strb[0]) begin
                $write("%c", core_req.w.data[7:0]);
                $fflush();
              end
            end
            if (core_req.w.last) begin
              state <= S_B;
            end else begin
              wr_addr_q <= wr_addr_q + {56'b0, 8'b1 << wr_size_q};
              wr_cnt_q  <= wr_cnt_q + 8'd1;
            end
          end
        end

        S_B: begin
          if (core_req.b_ready) state <= S_IDLE;
        end

        S_ATOM_R: begin
          if (GPCP_DBG)
            $display("[atomR] cyc=%0d rv=%b rr=%b id=%b rdata=%h oldq=%h",
                     dbg_cnt, core_rsp.r_valid, core_req.r_ready, core_rsp.r.id,
                     core_rsp.r.data, atom_old_q);
          if (core_req.r_ready) state <= S_ATOM_B;
        end

        S_ATOM_B: begin
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
    core_rsp.b_valid   = (state == S_B) || (state == S_ATOM_B);
    core_rsp.b.id      = wr_id_q;
    core_rsp.b.resp    = bresp_q;
    core_rsp.r_valid   = (state == S_RD) || (state == S_ATOM_R);
    core_rsp.r.data    = (state == S_ATOM_R) ? atom_old_q : rd_data_c;
    core_rsp.r.id      = (state == S_ATOM_R) ? wr_id_q : rd_id_q;
    core_rsp.r.resp    = (state == S_ATOM_R) ? axi_pkg::RESP_OKAY :
                         rd_excl_q ? axi_pkg::RESP_EXOKAY :
                         (rd_region_q == R_ERR) ? axi_pkg::RESP_SLVERR : axi_pkg::RESP_OKAY;
    core_rsp.r.last    = (state == S_ATOM_R) ? 1'b1 : (rd_cnt_q == rd_len_q);
  end

  assign ram_rdata = ram[ram_raddr];

  // ---------------------------------------------------------------- debug PC trace
  logic [63:0] last_pc;
  logic [63:0] dbg_cnt;
  logic [31:0] ar_cnt, aw_cnt, atop_cnt, commit_cnt;
  logic [63:0] last_ar_a, last_aw_a;
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      last_pc <= 64'b0;
      dbg_cnt <= 64'b0;
      ar_cnt  <= 32'b0;
      aw_cnt  <= 32'b0;
      atop_cnt<= 32'b0;
      commit_cnt <= 32'b0;
      last_ar_a <= 64'b0;
      last_aw_a <= 64'b0;
    end else begin
      dbg_cnt <= dbg_cnt + 64'd1;
      if (rvfi_probes.instr.commit_instr_valid[0])
        last_pc <= rvfi_probes.instr.commit_instr_pc[0];
      if (rvfi_probes.instr.commit_instr_valid != 0) commit_cnt <= commit_cnt + 1;
      if (ar_accept) begin ar_cnt <= ar_cnt + 1; last_ar_a <= core_req.ar.addr; end
      if (aw_accept) begin
        aw_cnt  <= aw_cnt + 1;
        last_aw_a <= core_req.aw.addr;
        if (core_req.aw.atop != 0) atop_cnt <= atop_cnt + 1;
      end
      if (GPCP_DBG && (dbg_cnt[15:0] == 16'hFFFF))
        $display("[soc] cyc=%0d pc=%h v=%b pc0=%h pc1=%h cmt=%0d ar=%0d aw=%0d atop=%0d",
                 dbg_cnt, last_pc, rvfi_probes.instr.commit_instr_valid,
                 rvfi_probes.instr.commit_instr_pc[0], rvfi_probes.instr.commit_instr_pc[1],
                 commit_cnt, ar_cnt, aw_cnt, atop_cnt);
    end
  end

endmodule

`endif
