// GPCP 的 CGRA-ME 驱动（库接口版；CGRA-ME 的 CLI 在本机读架构后会空转，见 docs/10 §3）
//
//   cgrame_map <arch.xml> <dfg.dot> <II> [ilp|anneal] [timelimit_s]
//
// 依赖仓库外的 CGRA-ME（EULA 禁止再分发）：$CGRA_ME_HOME（默认 ~/cgra-me）。
// 编译见 cgra/tools/map.sh。
#include <memory>
#include <map>
#include <cstdlib>
#include <string>
#include <fstream>
#include <iostream>
#include <CGRA/CGRA.h>
#include <CGRA/adlparse.h>
#include <CGRA/dotparse.h>
#include <CGRA/Mapper.h>
#include <CGRA/Mapping.h>

static std::string trim(std::string t)
{
    t.erase(0, t.find_first_not_of(" \t\r"));
    t.erase(t.find_last_not_of(" \t\r") + 1);
    return t;
}

int main(int argc, char** argv)
{
    if (argc < 4) {
        std::cerr << "usage: cgrame_map <arch.xml> <dfg.dot> <II> [ilp|anneal] [timelimit_s]\n";
        return 2;
    }
    bool use_ilp = (argc <= 4) || std::string(argv[4]) == "ilp";
    int timelimit = (argc > 5) ? std::atoi(argv[5]) : 120;

    std::string home = getenv("CGRA_ME_HOME") ? getenv("CGRA_ME_HOME") : std::string(getenv("HOME")) + "/cgra-me";

    std::cout << "[map] arch=" << argv[1] << "\n";
    auto arch = adl0::parseADL(argv[1]);
    std::cout << "[map] dfg=" << argv[2] << "\n";
    std::shared_ptr<OpGraph> opgraph = parseOpGraph(argv[2]);
    std::cout << "[map] ops=" << opgraph->op_nodes.size()
              << " vals=" << opgraph->val_nodes.size() << "\n";

    // mapper_config.ini 按 [section] 前缀展开成 "Section.key"
    std::map<std::string, std::string> args;
    std::ifstream ini(home + "/build/bin/mapper_config.ini");
    std::string line, section;
    while (std::getline(ini, line)) {
        if (line.empty() || line[0] == '#') continue;
        if (line[0] == '[') { section = trim(line.substr(1, line.find(']') - 1)); continue; }
        auto eq = line.find('=');
        if (eq == std::string::npos) continue;
        args[section + "." + trim(line.substr(0, eq))] = trim(line.substr(eq + 1));
    }
    args["ILPMapper.ilp_solver"] = "scip";   // 本机没有 Gurobi

    int II = std::atoi(argv[3]);
    std::cout << "[map] mapper=" << (use_ilp ? "ILP(SCIP)" : "Anneal") << " II=" << II
              << " timelimit=" << timelimit << "s\n";
    auto mapper = Mapper::createMapper(
        use_ilp ? MapperType::ILPMapper : MapperType::AnnealMapper, arch, timelimit, args);
    Mapping m = mapper->mapOpGraph(opgraph, II);
    std::cout << "[map] mapped=" << (m.isMapped() ? "yes" : "no") << " II=" << m.getII() << "\n";
    if (m.isMapped()) m.outputMapping();
    return m.isMapped() ? 0 : 1;
}
