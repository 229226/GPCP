// -----------------------------------------------------------------------------
// GPCP SoC testbench (milestone B2)
//
// Loads a bare-metal image into the SoC RAM through the backdoor port, releases
// the CVA6 core, and waits until the program writes the exit register.
//
// Usage:
//   ./Vgpcp_soc --prog prog.bin --max-cycles N --results 0x2000 --nwords 17 --dump out.txt
// -----------------------------------------------------------------------------
#include <verilated.h>
#include "Vgpcp_soc.h"

#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

static Vgpcp_soc* dut = nullptr;

static void tick() {
    dut->clk_i = 0;
    dut->eval();
    dut->clk_i = 1;
    dut->eval();
}

static std::vector<uint8_t> load_bin(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) { std::fprintf(stderr, "cannot open %s\n", path.c_str()); std::exit(1); }
    return std::vector<uint8_t>((std::istreambuf_iterator<char>(f)),
                                std::istreambuf_iterator<char>());
}

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);

    std::string prog_path, dump_path;
    uint64_t max_cycles = 20000000ull;
    uint32_t results_word = 0x400;   // 0x2000 / 8
    uint32_t nwords = 0;

    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) { std::fprintf(stderr, "missing value for %s\n", a.c_str()); std::exit(1); }
            return argv[++i];
        };
        if (a == "--prog") prog_path = next();
        else if (a == "--dump") dump_path = next();
        else if (a == "--max-cycles") max_cycles = std::strtoull(next().c_str(), nullptr, 0);
        else if (a == "--results") results_word = (uint32_t)std::strtoul(next().c_str(), nullptr, 0);
        else if (a == "--nwords") nwords = (uint32_t)std::strtoul(next().c_str(), nullptr, 0);
    }
    if (prog_path.empty()) {
        std::fprintf(stderr, "usage: %s --prog prog.bin [--max-cycles N] [--results word] [--nwords N] [--dump f]\n", argv[0]);
        return 1;
    }

    dut = new Vgpcp_soc;
    dut->clk_i = 0;
    dut->rst_ni = 0;
    dut->ram_we = 0;
    dut->eval();

    // ---- preload the program image while reset is asserted
    std::vector<uint8_t> img = load_bin(prog_path);
    if (img.size() > 512u * 1024u) {
        std::fprintf(stderr, "image too large: %zu bytes\n", img.size());
        return 2;
    }
    uint32_t words = (uint32_t)((img.size() + 7) / 8);
    img.resize(words * 8, 0);
    for (uint32_t w = 0; w < words; w++) {
        uint64_t v = 0;
        for (int b = 0; b < 8; b++) v |= (uint64_t)img[w * 8 + b] << (8 * b);
        dut->ram_we = 1;
        dut->ram_waddr = (uint16_t)w;
        dut->ram_wdata = v;
        tick();
        dut->ram_we = 0;
    }
    std::fprintf(stderr, "[tb] loaded %zu bytes (%u words) from %s\n", img.size(), words, prog_path.c_str());

    // ---- release reset and run
    for (int i = 0; i < 4; i++) tick();
    dut->rst_ni = 1;
    for (int i = 0; i < 4; i++) tick();

    uint64_t cycles = 0;
    while (!dut->exit_o && cycles < max_cycles) {
        tick();
        cycles++;
        if ((cycles % 1000000ull) == 0)
            std::fprintf(stderr, "[tb] %llu cycles...\n", (unsigned long long)cycles);
    }

    std::ostringstream os;
    os << "cycles " << cycles << "\n";
    if (!dut->exit_o) {
        os << "timeout 1\n";
    } else {
        os << "timeout 0\n";
        os << "exit_code " << std::hex << dut->exit_code_o << "\n";
    }

    for (uint32_t i = 0; i < nwords; i++) {
        dut->ram_raddr = (uint16_t)(results_word + i);
        dut->eval();
        std::printf("word %u 0x%016llx\n", i, (unsigned long long)dut->ram_rdata);
        os << "word " << i << " 0x" << std::hex << std::setw(16) << std::setfill('0')
           << (unsigned long long)dut->ram_rdata << std::dec << "\n";
    }

    if (!dump_path.empty()) {
        std::ofstream f(dump_path);
        f << os.str();
    }

    std::fprintf(stderr, "[tb] %s after %llu cycles\n",
                 dut->exit_o ? "exit" : "TIMEOUT", (unsigned long long)cycles);

    delete dut;
    return 0;
}
