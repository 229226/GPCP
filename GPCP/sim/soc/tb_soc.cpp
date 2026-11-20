// -----------------------------------------------------------------------------
// GPCP SoC testbench (B3: Linux)
//
// Loads one or more images into the SoC RAM through the backdoor port, releases
// the CVA6 core and runs until the program writes the simulation exit register
// (0x4001_0000) or the cycle budget is exhausted.
//
// Usage:
//   ./Vgpcp_soc --load fw.bin@0x80000000 --load kernel@0x80200000 \
//               --load dtb@0x82000000 --load initrd@0x84000000 \
//               --max-cycles N [--results 0x... --nwords N] [--dump out.txt]
// -----------------------------------------------------------------------------
#include <verilated.h>
#include "Vgpcp_soc.h"

#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <string>
#include <vector>
#include <utility>

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

struct Image { std::string path; uint64_t addr; };

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);

    std::vector<Image> images;
    std::string dump_path;
    uint64_t max_cycles = 200000000ull;
    uint64_t trace_every = 0;
    uint32_t results_addr = 0x80002000u;
    uint32_t nwords = 0;

    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) { std::fprintf(stderr, "missing value for %s\n", a.c_str()); std::exit(1); }
            return argv[++i];
        };
        if (a == "--load") {
            std::string spec = next();
            size_t at = spec.rfind('@');
            if (at == std::string::npos) { std::fprintf(stderr, "bad --load %s\n", spec.c_str()); return 1; }
            images.push_back({spec.substr(0, at), std::strtoull(spec.c_str() + at + 1, nullptr, 0)});
        } else if (a == "--dump") dump_path = next();
        else if (a == "--max-cycles") max_cycles = std::strtoull(next().c_str(), nullptr, 0);
        else if (a == "--results") results_addr = (uint32_t)std::strtoul(next().c_str(), nullptr, 0);
        else if (a == "--nwords") nwords = (uint32_t)std::strtoul(next().c_str(), nullptr, 0);
        else if (a == "--trace-pc") trace_every = std::strtoull(next().c_str(), nullptr, 0);
    }
    if (images.empty()) {
        std::fprintf(stderr, "no images; use --load file@addr\n");
        return 1;
    }

    dut = new Vgpcp_soc;
    dut->clk_i = 0;
    dut->rst_ni = 0;
    dut->ram_we = 0;
    dut->eval();

    // ---- preload images while reset is asserted
    for (const auto& img : images) {
        if (img.addr & 0x7ull) {
            std::fprintf(stderr, "[tb] %s: address 0x%llx is not 8-byte aligned\n",
                         img.path.c_str(), (unsigned long long)img.addr);
            return 2;
        }
        std::vector<uint8_t> data = load_bin(img.path);
        uint32_t words = (uint32_t)((data.size() + 7) / 8);
        data.resize(words * 8, 0);
        uint32_t base = (uint32_t)(img.addr >> 3);
        for (uint32_t w = 0; w < words; w++) {
            uint64_t v = 0;
            for (int b = 0; b < 8; b++) v |= (uint64_t)data[w * 8 + b] << (8 * b);
            dut->ram_we = 1;
            dut->ram_waddr = (uint32_t)(base + w) & 0x7FFFFF;
            dut->ram_wdata = v;
            tick();
        }
        dut->ram_we = 0;
        std::fprintf(stderr, "[tb] loaded %-40s %8zu bytes @ 0x%08llx\n",
                     img.path.c_str(), data.size(), (unsigned long long)img.addr);
    }

    // ---- release reset
    for (int i = 0; i < 4; i++) tick();
    dut->rst_ni = 1;
    for (int i = 0; i < 4; i++) tick();

    uint64_t cycles = 0;
    while (!dut->exit_o && cycles < max_cycles) {
        tick();
        cycles++;
        if ((cycles % 10000000ull) == 0)
            std::fprintf(stderr, "[tb] %llu cycles...\n", (unsigned long long)cycles);
        if (trace_every && (cycles % trace_every) == 0)
            std::fprintf(stderr, "[pc] cyc=%llu pc=0x%016llx\n",
                         (unsigned long long)cycles, (unsigned long long)dut->dbg_pc_o);
    }

    std::ostringstream os;
    os << "cycles " << cycles << "\n";
    if (!dut->exit_o) {
        os << "timeout 1\n";
    } else {
        os << "timeout 0\n";
        os << "exit_code " << std::hex << dut->exit_code_o << "\n";
    }

    // let any store still resident in the write-through buffer land before the
    // backdoor reads the result area
    for (int i = 0; i < 256; i++) tick();

    for (uint32_t i = 0; i < nwords; i++) {
        dut->ram_raddr = (uint32_t)((results_addr >> 3) + i) & 0x7FFFFF;
        dut->eval();
        std::printf("word %u 0x%016llx\n", i, (unsigned long long)dut->ram_rdata);
        os << "word " << i << " 0x" << std::hex << std::setw(16) << std::setfill('0')
           << (unsigned long long)dut->ram_rdata << std::dec << "\n";
    }

    if (!dump_path.empty()) {
        std::ofstream f(dump_path);
        f << os.str();
    }

    std::fprintf(stderr, "\n[tb] %s after %llu cycles\n",
                 dut->exit_o ? "exit" : "TIMEOUT", (unsigned long long)cycles);

    delete dut;
    return 0;
}
