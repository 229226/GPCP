#!/usr/bin/env python3
"""Generate the Verilator file list for the CVA6 subset used by the GPCP SoC."""

from pathlib import Path
root = Path('third_party/cva6')
pkgs = [
 'core/include/config_pkg.sv',
 'core/include/cv64a6_imafdc_sv39_config_pkg.sv',
 'core/include/build_config_pkg.sv',
 'core/include/riscv_pkg.sv',
 'core/include/aes_pkg.sv',
 'core/include/dummy_l15_pkg.sv',
 'corev_apu/riscv-dbg/src/dm_pkg.sv',
 'core/cvfpu/src/fpnew_pkg.sv',
 'core/include/ariane_pkg.sv',
 'vendor/pulp-platform/axi/src/axi_pkg.sv',
 'corev_apu/tb/ariane_axi_pkg.sv',
 'core/include/wt_cache_pkg.sv',
 'core/include/std_cache_pkg.sv',
 'vendor/pulp-platform/common_cells/src/cb_filter_pkg.sv',
 'vendor/pulp-platform/common_cells/src/cf_math_pkg.sv',
 'vendor/pulp-platform/common_cells/src/ecc_pkg.sv',
 'core/include/instr_tracer_pkg.sv',
]
dirs = ['core','corev_apu/src','core/cvfpu/src','common',
        'vendor/pulp-platform/common_cells/src',
        'vendor/pulp-platform/tech_cells_generic/src',
        'vendor/pulp-platform/axi/include']
EXCL = ['/hpdcache/','/cva6_hpdcache','/cvxif_example/','/tb/','/deprecated/','/tech_cells_generic/src/fpga/','tc_sram_fpga_wrapper.sv']
files=[root/p for p in pkgs]; seen={str(f) for f in files}
for d in dirs:
    for ext in ('*.sv','*.v'):
        for f in sorted((root/d).rglob(ext)):
            s=str(f)
            if s in seen: continue
            if any(x in s for x in EXCL): continue
            name=f.name
            if 'core/include/' in s and 'config_pkg' in name and name not in ('config_pkg.sv','build_config_pkg.sv','cv64a6_imafdc_sv39_config_pkg.sv'):
                continue
            seen.add(s); files.append(f)
Path('sim/soc/cva6.f').write_text('\n'.join(str(f) for f in files)+'\n')
print(len(files),'files')
