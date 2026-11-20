#!/usr/bin/env python3
"""给 Morpher 的映射器加一个调度导出（schedule.csv）。

Morpher 只输出 HyCUBE 专用位流，我们拿不到"哪个算子在第几拍、哪个 PE"。
这个补丁把 (t, x, y, dfg_idx, op) 写成 CSV，作为我们后端的输入。
"""

import os
import sys

ROOT = os.path.expanduser("~/morpher/Morpher_CGRA_Mapper")

HDR = os.path.join(ROOT, "include/morpher/mapper/PathFinderMapper.h")
CPP = os.path.join(ROOT, "src/mapper/PathFinderMapper.cpp")
CALL = os.path.join(ROOT, "src/CGRA_xml_compiler.cpp")

DECL = "\tvoid printHyCUBEBinary(CGRA* cgra);"

FN = '''void CGRAXMLCompile::PathFinderMapper::dumpSchedule(CGRA* cgra, std::string fName) {
\tstd::ofstream out(fName.c_str());
\tout << "t,x,y,idx,op" << std::endl;
\tfor (int t = 0; t < cgra->get_t_max(); ++t) {
\t\tvector<PE *> peList = this->cgra->getSpatialPEList(t);
\t\tfor (PE *pe : peList) {
\t\t\tFU* fu = static_cast<FU*>(pe->getSubMod("FU0"));
\t\t\tif (!fu) continue;
\t\t\tDataPath* dp = static_cast<DataPath*>(fu->getSubMod("DP0"));
\t\t\tif (!dp) continue;
\t\t\tDFGNode* n = dp->getMappedNode();
\t\t\tif (!n) continue;
\t\t\tout << t << "," << pe->X << "," << pe->Y << ","
\t\t\t    << n->idx << "," << n->op << std::endl;
\t\t}
\t}
\tout.close();
\tstd::cout << "Schedule written to " << fName << std::endl;
}

'''

ANCHOR = "void CGRAXMLCompile::PathFinderMapper::printHyCUBEBinary(CGRA* cgra) {"
CALL_OLD = "\t\t\t\tmapper->printHyCUBEBinary(tempCGRA);"
CALL_NEW = (CALL_OLD +
            "\n\t\t\t\tmapper->dumpSchedule(tempCGRA, \"schedule.csv\");")


def patch(path, old, new, marker):
    s = open(path, encoding="utf-8", errors="replace").read()
    if marker in s:
        print("  %-28s already patched" % os.path.basename(path))
        return
    if old not in s:
        sys.exit("anchor not found in %s" % path)
    s = s.replace(old, new, 1)
    open(path, "w", encoding="utf-8").write(s)
    print("  %-28s patched" % os.path.basename(path))


patch(HDR, DECL,
      DECL + "\n\tvoid dumpSchedule(CGRA* cgra, std::string fName);", "dumpSchedule")
patch(CPP, ANCHOR, FN + ANCHOR, "dumpSchedule")
patch(CALL, CALL_OLD, CALL_NEW, "dumpSchedule")
print("done")
