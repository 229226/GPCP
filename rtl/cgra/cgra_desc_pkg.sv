// 由 tools/gen/gen_cgra_desc.py 从 rtl/cgra/cgra_desc64.yml 生成，不要手改。
// 64B 复合描述符字段访问器：每个字段只在 YAML 里定义一次。
package cgra_desc_pkg;

  // src: word 0 [31:0]（beat0 [31:0]）
  function automatic logic [31:0] d64_src(input logic [63:0] b);
    return b[31:0];
  endfunction

  // spad: word 1 [15:0]（beat0 [47:32]）
  function automatic logic [15:0] d64_spad(input logic [63:0] b);
    return b[47:32];
  endfunction

  // page: word 1 [16:16]（beat0 [48:48]）
  function automatic logic [0:0] d64_page(input logic [63:0] b);
    return b[48:48];
  endfunction

  // dir: word 1 [17:17]（beat0 [49:49]）
  function automatic logic [0:0] d64_dir(input logic [63:0] b);
    return b[49:49];
  endfunction

  // irq: word 1 [18:18]（beat0 [50:50]）
  function automatic logic [0:0] d64_irq(input logic [63:0] b);
    return b[50:50];
  endfunction

  // tgt_rot: word 1 [19:19]（beat0 [51:51]）
  function automatic logic [0:0] d64_tgt_rot(input logic [63:0] b);
    return b[51:51];
  endfunction

  // in_len: word 2 [15:0]（beat1 [15:0]）
  function automatic logic [15:0] d64_in_len(input logic [63:0] b);
    return b[15:0];
  endfunction

  // stride: word 3 [15:0]（beat1 [47:32]）
  function automatic logic [15:0] d64_stride(input logic [63:0] b);
    return b[47:32];
  endfunction

  // row: word 3 [31:16]（beat1 [63:48]）
  function automatic logic [15:0] d64_row(input logic [63:0] b);
    return b[63:48];
  endfunction

  // cycles: word 6 [15:0]（beat3 [15:0]）
  function automatic logic [15:0] d64_cycles(input logic [63:0] b);
    return b[15:0];
  endfunction

  // arr_page: word 6 [16:16]（beat3 [16:16]）
  function automatic logic [0:0] d64_arr_page(input logic [63:0] b);
    return b[16:16];
  endfunction

  // arr_imem: word 6 [17:17]（beat3 [17:17]）
  function automatic logic [0:0] d64_arr_imem(input logic [63:0] b);
    return b[17:17];
  endfunction

  // arr_en: word 6 [31:31]（beat3 [31:31]）
  function automatic logic [0:0] d64_arr_en(input logic [63:0] b);
    return b[31:31];
  endfunction

  // out_len: word 8 [15:0]（beat4 [15:0]）
  function automatic logic [15:0] d64_out_len(input logic [63:0] b);
    return b[15:0];
  endfunction

  // out_spad: word 8 [31:16]（beat4 [31:16]）
  function automatic logic [15:0] d64_out_spad(input logic [63:0] b);
    return b[31:16];
  endfunction

  // out_en: word 9 [0:0]（beat4 [32:32]）
  function automatic logic [0:0] d64_out_en(input logic [63:0] b);
    return b[32:32];
  endfunction

  // out_dst: word 10 [31:0]（beat5 [31:0]）
  function automatic logic [31:0] d64_out_dst(input logic [63:0] b);
    return b[31:0];
  endfunction

  // rep: word 12 [15:0]（beat6 [15:0]）
  function automatic logic [15:0] d64_rep(input logic [63:0] b);
    return b[15:0];
  endfunction

  // in_step: word 12 [31:16]（beat6 [31:16]）
  function automatic logic [15:0] d64_in_step(input logic [63:0] b);
    return b[31:16];
  endfunction

  // out_step: word 13 [15:0]（beat6 [47:32]）
  function automatic logic [15:0] d64_out_step(input logic [63:0] b);
    return b[47:32];
  endfunction

  // pf_src: word 14 [31:0]（beat7 [31:0]）
  function automatic logic [31:0] d64_pf_src(input logic [63:0] b);
    return b[31:0];
  endfunction

  // pf_len: word 15 [15:0]（beat7 [47:32]）
  function automatic logic [15:0] d64_pf_len(input logic [63:0] b);
    return b[47:32];
  endfunction

  // pf_spad: word 15 [31:16]（beat7 [63:48]）
  function automatic logic [15:0] d64_pf_spad(input logic [63:0] b);
    return b[63:48];
  endfunction

  // pf_page: word 7 [0:0]（beat3 [32:32]）
  function automatic logic [0:0] d64_pf_page(input logic [63:0] b);
    return b[32:32];
  endfunction

  // pf_stride: word 7 [31:16]（beat3 [63:48]）
  function automatic logic [15:0] d64_pf_stride(input logic [63:0] b);
    return b[63:48];
  endfunction

  // pf_row: word 11 [15:0]（beat5 [47:32]）
  function automatic logic [15:0] d64_pf_row(input logic [63:0] b);
    return b[47:32];
  endfunction

  // pf_in: word 11 [30:30]（beat5 [62:62]）
  function automatic logic [0:0] d64_pf_in(input logic [63:0] b);
    return b[62:62];
  endfunction

  // pf_en: word 11 [31:31]（beat5 [63:63]）
  function automatic logic [0:0] d64_pf_en(input logic [63:0] b);
    return b[63:63];
  endfunction

endpackage
