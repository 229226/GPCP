// -----------------------------------------------------------------------------
// GPCP CGRA - Verilator testbench
//
// This harness plays the role of the "CPU" (bus master) for milestone M0.
// It drives the AXI4-Lite slave exactly as the RISC-V core will later:
//   - load microcode into the CGRA instruction memory
//   - preload PE registers / accumulators
//   - start the sequencer
//   - stream samples into `din`
//   - read back PE state
//
// Usage:
//   ./Vcgra_axi_lite --prog prog.hex --cycles N [--preload pre.txt]
//                    [--din din.txt] [--dump dump.txt]
// -----------------------------------------------------------------------------
#include <verilated.h>
#include <Vcgra_axi_lite.h>

#include <array>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

static Vcgra_axi_lite* dut = nullptr;
static std::vector<uint32_t> g_din;
static bool g_stream = false;
static std::vector<uint32_t> g_dma_mem(1024, 0);   // copy-engine backing store (standalone, 32-bit words)

// ---------------------------------------------------------------------------
// Tiny AXI4 slave for the copy-engine master: one outstanding INCR burst,
// 64-bit beats.  The model answers combinationally in the same cycle.
// ---------------------------------------------------------------------------
struct AxiSlave {
    bool     r_active = false;
    uint32_t r_addr = 0, r_beat = 0, r_len = 0;
    bool     w_active = false;
    uint32_t w_addr = 0, w_beat = 0, w_len = 0;
    bool     b_pending = false;
} g_slv;

static uint32_t mem_w(uint32_t byte_addr) { return (byte_addr >> 2) & 0x3FF; }

static uint64_t mem_ld64(uint32_t byte_addr) {
    return (uint64_t)g_dma_mem[mem_w(byte_addr)] |
           ((uint64_t)g_dma_mem[mem_w(byte_addr + 4)] << 32);
}

static void axi_slave_drive() {
    dut->m_axi_arready = !g_slv.r_active;
    dut->m_axi_awready = !g_slv.w_active && !g_slv.b_pending;
    dut->m_axi_wready  = g_slv.w_active;
    dut->m_axi_rvalid  = g_slv.r_active;
    dut->m_axi_rlast   = g_slv.r_active && (g_slv.r_beat == g_slv.r_len);
    dut->m_axi_rdata   = g_slv.r_active ? mem_ld64(g_slv.r_addr) : 0;
    dut->m_axi_bvalid  = g_slv.b_pending;
    dut->m_axi_bid     = 0;
    dut->m_axi_bresp   = 0;
    dut->m_axi_rid     = 0;
    dut->m_axi_rresp   = 0;
}

static void axi_slave_update() {
    if (dut->m_axi_awvalid && dut->m_axi_awready) {
        g_slv.w_addr = dut->m_axi_awaddr;
        g_slv.w_len  = dut->m_axi_awlen;
        g_slv.w_beat = 0;
        g_slv.w_active = true;
    }
    if (dut->m_axi_wvalid && dut->m_axi_wready) {
        uint8_t st = dut->m_axi_wstrb;
        if (st & 0x0F) g_dma_mem[mem_w(g_slv.w_addr)] = (uint32_t)dut->m_axi_wdata;
        if (st & 0xF0) g_dma_mem[mem_w(g_slv.w_addr + 4)] = (uint32_t)(dut->m_axi_wdata >> 32);
        g_slv.w_addr += 8;
        if (dut->m_axi_wlast) {
            g_slv.w_active = false;
            g_slv.b_pending = true;
        } else {
            g_slv.w_beat++;
        }
    }
    if (dut->m_axi_bvalid && dut->m_axi_bready) g_slv.b_pending = false;

    if (dut->m_axi_arvalid && dut->m_axi_arready) {
        g_slv.r_addr = dut->m_axi_araddr;
        g_slv.r_len  = dut->m_axi_arlen;
        g_slv.r_beat = 0;
        g_slv.r_active = true;
    }
    if (dut->m_axi_rvalid && dut->m_axi_rready) {
        g_slv.r_addr += 8;
        if (g_slv.r_beat == g_slv.r_len) g_slv.r_active = false;
        else g_slv.r_beat++;
    }
}

static void tick() {
    dut->clk = 0;
    dut->eval();
    // drive the streaming input so that it is stable before the rising edge
    if (g_stream)
        dut->din = dut->o_busy ? g_din[(dut->o_cycle < g_din.size()) ? dut->o_cycle : (g_din.size() - 1)] : 0;
    else
        dut->din = 0;
    axi_slave_drive();
    dut->eval();                 // let the design see the slave's responses
    axi_slave_update();          // resolve the handshakes of this cycle
    dut->clk = 1;
    dut->eval();
}

static void reset_dut() {
    dut->rst_n = 0;
    dut->clk = 0;
    dut->eval();
    for (int i = 0; i < 8; i++) tick();
    dut->rst_n = 1;
    tick();
}

static void axil_wr(uint32_t addr, uint32_t data) {
    dut->s_axil_awaddr = addr & 0xFFFF;
    dut->s_axil_awvalid = 1;
    dut->s_axil_wdata = data;
    dut->s_axil_wstrb = 0xF;
    dut->s_axil_wvalid = 1;
    dut->s_axil_bready = 1;
    dut->eval();
    tick();  // accept edge
    dut->s_axil_awvalid = 0;
    dut->s_axil_wvalid = 0;
    for (int i = 0; i < 16 && !dut->s_axil_bvalid; i++) tick();
    for (int i = 0; i < 16 && dut->s_axil_bvalid; i++) tick();
}

static uint32_t axil_rd(uint32_t addr) {
    dut->s_axil_araddr = addr & 0xFFFF;
    dut->s_axil_arvalid = 1;
    dut->s_axil_rready = 1;
    dut->eval();
    tick();  // accept edge, rdata/rvalid registered
    uint32_t v = dut->s_axil_rdata;
    dut->s_axil_arvalid = 0;
    tick();  // consume response
    return v;
}

static std::vector<uint32_t> load_words(const std::string& path) {
    std::vector<uint32_t> v;
    std::ifstream f(path);
    if (!f) {
        std::fprintf(stderr, "cannot open %s\n", path.c_str());
        std::exit(1);
    }
    std::string line;
    while (std::getline(f, line)) {
        if (line.empty() || line[0] == '#') continue;
        // the toolchain always emits 0x-prefixed values; parse them as hex
        v.push_back((uint32_t)strtoul(line.c_str(), nullptr, 16));
    }
    return v;
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);

    std::string prog_path, preload_path, din_path, dump_path, dma_mem_path, dma_dump_path, dma_ops_path;
    std::string ring_path;
    uint32_t ring_base = 0x00000200;   // byte address, 8-byte aligned, inside the 4 KiB model
    uint32_t ring_size = 1024;         // like the SoC: a deep ring, head reset per batch
    int buf_page = 0;
    int cycles = 0;
    uint32_t spad_start = 0;
    int spad_count = 0;

    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) { std::fprintf(stderr, "missing value for %s\n", a.c_str()); std::exit(1); }
            return argv[++i];
        };
        if (a == "--prog") prog_path = next();
        else if (a == "--preload") preload_path = next();
        else if (a == "--din") din_path = next();
        else if (a == "--dump") dump_path = next();
        else if (a == "--dma-mem") dma_mem_path = next();
        else if (a == "--dma-dump") dma_dump_path = next();
        else if (a == "--dma-ops") dma_ops_path = next();
        else if (a == "--ring-desc") ring_path = next();
        else if (a == "--ring-base") ring_base = (uint32_t)strtoul(next().c_str(), nullptr, 0);
        else if (a == "--ring-size") ring_size = (uint32_t)strtoul(next().c_str(), nullptr, 0);
        else if (a == "--buf-page") buf_page = std::atoi(next().c_str());
        else if (a == "--cycles") cycles = std::atoi(next().c_str());
        else if (a == "--spad-dump") {
            spad_start = (uint32_t)strtoul(next().c_str(), nullptr, 0);
            spad_count = std::atoi(next().c_str());
        }
    }
    if (prog_path.empty() || cycles <= 0) {
        std::fprintf(stderr, "usage: %s --prog prog.hex --cycles N [--preload p.txt] [--din d.txt] [--dump out.txt]\n", argv[0]);
        return 1;
    }

    dut = new Vcgra_axi_lite;
    reset_dut();

    // sanity check on the ID register
    uint32_t id = axil_rd(0x000C);
    if (((id >> 24) & 0xFF) != 0xCA) {
        std::fprintf(stderr, "unexpected CGRA ID 0x%08x\n", id);
        return 2;
    }

    // 1. instruction memory
    std::vector<uint32_t> prog = load_words(prog_path);
    size_t need = (size_t)cycles * 16;
    if (prog.size() < need) {
        std::fprintf(stderr, "program too short: %zu < %zu words\n", prog.size(), need);
        return 3;
    }
    for (size_t i = 0; i < need; i++)
        axil_wr(0x2000 + (uint32_t)(i * 4), prog[i]);

    // 2. preload PE registers
    if (!preload_path.empty()) {
        std::ifstream f(preload_path);
        if (!f) { std::fprintf(stderr, "cannot open %s\n", preload_path.c_str()); return 4; }
        uint32_t addr, val;
        while (f >> std::hex >> addr >> val)
            axil_wr(addr, val);
    }

    // 2b. copy-engine backing memory
    if (!dma_mem_path.empty()) {
        std::vector<uint32_t> words = load_words(dma_mem_path);
        for (size_t i = 0; i < words.size() && i < g_dma_mem.size(); i++)
            g_dma_mem[i] = words[i];
    }

    // 3. streaming input
    if (!din_path.empty()) {
        g_din = load_words(din_path);
        g_stream = true;
    }

    // 3b. copy-engine descriptors (src, dst, len, page, dir, stride, row).
    //     Numbers may be written in decimal or with a 0x prefix - streams need
    //     per-token parsing, the plain `>>` would stop at the 'x'.
    if (!dma_ops_path.empty()) {
        std::ifstream f(dma_ops_path);
        std::string line;
        while (std::getline(f, line)) {
            std::istringstream ss(line);
            std::vector<uint32_t> t;
            std::string tok;
            while (ss >> tok) t.push_back((uint32_t)strtoul(tok.c_str(), nullptr, 0));
            if (t.size() < 5) continue;
            uint32_t src = t[0], dst = t[1], len = t[2], page = t[3], dir = t[4];
            uint32_t stride = 1, row = 0;
            if (t.size() > 5) stride = t[5];
            if (t.size() > 6) row = t[6];
            axil_wr(0x0010, src);
            axil_wr(0x0014, dst);
            axil_wr(0x0018, len);
            axil_wr(0x0024, page);
            axil_wr(0x0028, stride);
            axil_wr(0x002C, row);
            axil_wr(0x001C, 0x1 | (dir << 1));
            int guard = 0;
            while (((axil_rd(0x001C) & 0x2) == 0) && guard++ < 100000) ;
            if (guard >= 100000) std::fprintf(stderr, "timeout: single-shot dma\n");
        }
    }

    // 3c. descriptor ring: each line holds one 32-byte descriptor (8 words).
    //     Descriptors are placed at --ring-base inside the slave memory model,
    //     the ring is then walked by the engine exactly as the SoC does it.
    if (!ring_path.empty()) {
        std::ifstream f(ring_path);
        std::vector<std::array<uint32_t, 8>> descs;
        std::string line;
        while (std::getline(f, line)) {
            std::istringstream ss(line);
            std::array<uint32_t, 8> d{};
            std::string tok;
            size_t n = 0;
            while (ss >> tok) {
                if (n < d.size()) d[n] = (uint32_t)strtoul(tok.c_str(), nullptr, 0);
                n++;
            }
            if (n >= d.size()) descs.push_back(d);
        }
        if (descs.empty()) {
            std::fprintf(stderr, "no descriptors in %s\n", ring_path.c_str());
            return 6;
        }
        uint32_t base_w = mem_w(ring_base);
        for (size_t i = 0; i < descs.size(); i++)
            for (size_t k = 0; k < 8; k++)
                g_dma_mem[(base_w + (uint32_t)(i * 8 + k)) & 0x3FF] = descs[i][k];
        axil_wr(0x0048, ring_base);
        axil_wr(0x004C, ring_size);
        axil_wr(0x0050, 0);
        axil_wr(0x0054, (uint32_t)descs.size());
        axil_wr(0x0058, 0x1);
        int guard = 0;
        while (((axil_rd(0x005C) & 0xFFFF) < (uint32_t)descs.size()) && guard++ < 100000) ;
        if (guard >= 100000) std::fprintf(stderr, "timeout: descriptor ring\n");
        axil_wr(0x0058, 0x0);
        std::fprintf(stderr, "[ring] %zu descriptors, size=%u, done=%u\n", descs.size(),
                     ring_size, axil_rd(0x005C) & 0xFFFF);
    }
    axil_wr(0x0020, (uint32_t)buf_page);  // array page

    // 4. run
    axil_wr(0x0004, (uint32_t)cycles);  // NUM_CYCLES
    axil_wr(0x0000, 0x1);               // CTRL.start

    int guard = 0;
    while (dut->o_busy && guard++ < 100000000) tick();
    if (dut->o_busy) {
        std::fprintf(stderr, "timeout waiting for CGRA done\n");
        return 5;
    }
    // let the last cycle settle
    tick();

    // 5. dump state
    std::ostringstream os;
    os << "cycles " << (int)dut->o_cycle + 1 << "\n";
    char buf[64];
    for (int p = 0; p < 16; p++) {
        for (int r = 0; r < 8; r++) {
            uint32_t addr = 0x1000 + p * 32 + r * 4;
            std::snprintf(buf, sizeof(buf), "pe %d r%d 0x%08x\n", p, r, axil_rd(addr));
            os << buf;
        }
        std::snprintf(buf, sizeof(buf), "pe %d acc 0x%08x\n", p, axil_rd(0x1200 + p * 4));
        os << buf;
    }

    for (int i = 0; i < spad_count; i++) {
        uint32_t addr = spad_start + (uint32_t)i * 4;
        std::snprintf(buf, sizeof(buf), "spad %04x 0x%08x\n", addr, axil_rd(addr));
        os << buf;
    }

    if (!dma_dump_path.empty()) {
        std::ofstream f(dma_dump_path);
        for (size_t i = 0; i < g_dma_mem.size(); i++)
            f << "0x" << std::hex << g_dma_mem[i] << "\n";
    }

    if (!dump_path.empty()) {
        std::ofstream f(dump_path);
        f << os.str();
    } else {
        std::cout << os.str();
    }

    delete dut;
    return 0;
}
