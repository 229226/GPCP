// -----------------------------------------------------------------------------
// GPCP CGRA - AXI4-Lite slave wrapper
//
// Single outstanding transaction, full 32-bit writes (wstrb ignored).
// The CGRA host write port expects a single-cycle pulse; the wrapper converts
// an AXI write handshake into exactly one wr_en pulse.
// -----------------------------------------------------------------------------
`ifndef CGRA_AXI_LITE_SV
`define CGRA_AXI_LITE_SV

module cgra_axi_lite (
    input  logic        clk,
    input  logic        rst_n,
    // AXI4-Lite slave
    input  logic [15:0] s_axil_awaddr,
    input  logic        s_axil_awvalid,
    output logic        s_axil_awready,
    input  logic [31:0] s_axil_wdata,
    input  logic [3:0]  s_axil_wstrb,
    input  logic        s_axil_wvalid,
    output logic        s_axil_wready,
    output logic [1:0]  s_axil_bresp,
    output logic        s_axil_bvalid,
    input  logic        s_axil_bready,
    input  logic [15:0] s_axil_araddr,
    input  logic        s_axil_arvalid,
    output logic        s_axil_arready,
    output logic [31:0] s_axil_rdata,
    output logic [1:0]  s_axil_rresp,
    output logic        s_axil_rvalid,
    input  logic        s_axil_rready,
    // AXI4 master: copy engine (tie off when used stand-alone)
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
    // streaming input / status
    input  logic [31:0] din,
    output logic        o_busy,
    output logic        o_done,
    output logic [15:0] o_cycle,
    output logic        o_irq
);

  // ---------------- write channel ----------------
  typedef enum logic [0:0] {W_IDLE = 1'b0, W_RESP = 1'b1} wstate_e;
  wstate_e wstate;

  logic        wr_en_q;
  logic [15:0] wr_addr_q;
  logic [31:0] wr_data_q;

  // ready only when both address and data are present, so that the master
  // never sees a second handshake for the same transaction
  assign s_axil_awready = (wstate == W_IDLE) && s_axil_awvalid && s_axil_wvalid;
  assign s_axil_wready  = (wstate == W_IDLE) && s_axil_awvalid && s_axil_wvalid;
  assign s_axil_bresp   = 2'b00;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      wstate     <= W_IDLE;
      wr_en_q    <= 1'b0;
      wr_addr_q  <= 16'b0;
      wr_data_q  <= 32'b0;
      s_axil_bvalid <= 1'b0;
    end else begin
      wr_en_q <= 1'b0;
      unique case (wstate)
        W_IDLE: begin
          if (s_axil_awvalid && s_axil_wvalid) begin
            wr_en_q   <= 1'b1;
            wr_addr_q <= s_axil_awaddr;
            wr_data_q <= s_axil_wdata;
            wstate    <= W_RESP;
          end
        end
        W_RESP: begin
          if (!s_axil_bvalid) s_axil_bvalid <= 1'b1;
          else if (s_axil_bready) begin
            s_axil_bvalid <= 1'b0;
            wstate        <= W_IDLE;
          end
        end
      endcase
    end
  end

  // ---------------- read channel ----------------
  typedef enum logic [0:0] {R_IDLE = 1'b0, R_RESP = 1'b1} rstate_e;
  rstate_e rstate;

  logic [31:0] host_rd_data;

  assign s_axil_arready = (rstate == R_IDLE) && s_axil_arvalid;
  assign s_axil_rresp   = 2'b00;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      rstate        <= R_IDLE;
      s_axil_rvalid <= 1'b0;
      s_axil_rdata  <= 32'b0;
    end else begin
      unique case (rstate)
        R_IDLE: begin
          if (s_axil_arvalid) begin
            s_axil_rdata  <= host_rd_data;
            s_axil_rvalid <= 1'b1;
            rstate        <= R_RESP;
          end
        end
        R_RESP: begin
          if (s_axil_rvalid && s_axil_rready) begin
            s_axil_rvalid <= 1'b0;
            rstate        <= R_IDLE;
          end
        end
      endcase
    end
  end

  // ---------------- CGRA core ----------------
  cgra_top u_core (
      .clk    (clk),
      .rst_n  (rst_n),
      .wr_en  (wr_en_q),
      .wr_addr(wr_addr_q),
      .wr_data(wr_data_q),
      .rd_addr(s_axil_araddr),
      .rd_data(host_rd_data),
      .din    (din),
      .m_axi_awvalid(m_axi_awvalid), .m_axi_awready(m_axi_awready),
      .m_axi_awid(m_axi_awid), .m_axi_awaddr(m_axi_awaddr),
      .m_axi_awlen(m_axi_awlen), .m_axi_awsize(m_axi_awsize),
      .m_axi_awburst(m_axi_awburst),
      .m_axi_wvalid(m_axi_wvalid), .m_axi_wready(m_axi_wready),
      .m_axi_wdata(m_axi_wdata), .m_axi_wstrb(m_axi_wstrb), .m_axi_wlast(m_axi_wlast),
      .m_axi_bvalid(m_axi_bvalid), .m_axi_bready(m_axi_bready),
      .m_axi_bid(m_axi_bid), .m_axi_bresp(m_axi_bresp),
      .m_axi_arvalid(m_axi_arvalid), .m_axi_arready(m_axi_arready),
      .m_axi_arid(m_axi_arid), .m_axi_araddr(m_axi_araddr),
      .m_axi_arlen(m_axi_arlen), .m_axi_arsize(m_axi_arsize),
      .m_axi_arburst(m_axi_arburst),
      .m_axi_rvalid(m_axi_rvalid), .m_axi_rready(m_axi_rready),
      .m_axi_rdata(m_axi_rdata), .m_axi_rlast(m_axi_rlast),
      .m_axi_rid(m_axi_rid), .m_axi_rresp(m_axi_rresp),
      .busy   (o_busy),
      .done   (o_done),
      .cycle_o(o_cycle),
      .irq_o  (o_irq),
      .dbg_soc_state(4'd0)
  );

endmodule

`endif
