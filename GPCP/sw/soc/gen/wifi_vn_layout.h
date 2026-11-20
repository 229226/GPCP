/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: wifi_vn；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef WIFI_VN_LAYOUT_H
#define WIFI_VN_LAYOUT_H

#define WIFI_VN_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define WIFI_VN_LAYOUT_NPES        16u
#define WIFI_VN_LAYOUT_WORDS       32u
#define WIFI_VN_LAYOUT_BANKS       32u

#define WIFI_VN_SPAD_LEAF_CH_BASE   24u
#define WIFI_VN_SPAD_LEAF_CH_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_CH(i)     (24u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M0_BASE   0u
#define WIFI_VN_SPAD_LEAF_M0_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M0(i)     (0u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M1_BASE   1u
#define WIFI_VN_SPAD_LEAF_M1_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M1(i)     (1u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M10_BASE   10u
#define WIFI_VN_SPAD_LEAF_M10_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M10(i)     (10u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M11_BASE   11u
#define WIFI_VN_SPAD_LEAF_M11_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M11(i)     (11u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M12_BASE   12u
#define WIFI_VN_SPAD_LEAF_M12_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M12(i)     (12u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M13_BASE   13u
#define WIFI_VN_SPAD_LEAF_M13_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M13(i)     (13u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M14_BASE   14u
#define WIFI_VN_SPAD_LEAF_M14_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M14(i)     (14u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M15_BASE   15u
#define WIFI_VN_SPAD_LEAF_M15_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M15(i)     (15u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M16_BASE   16u
#define WIFI_VN_SPAD_LEAF_M16_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M16(i)     (16u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M17_BASE   17u
#define WIFI_VN_SPAD_LEAF_M17_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M17(i)     (17u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M18_BASE   18u
#define WIFI_VN_SPAD_LEAF_M18_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M18(i)     (18u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M19_BASE   19u
#define WIFI_VN_SPAD_LEAF_M19_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M19(i)     (19u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M2_BASE   2u
#define WIFI_VN_SPAD_LEAF_M2_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M2(i)     (2u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M20_BASE   20u
#define WIFI_VN_SPAD_LEAF_M20_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M20(i)     (20u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M21_BASE   21u
#define WIFI_VN_SPAD_LEAF_M21_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M21(i)     (21u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M22_BASE   22u
#define WIFI_VN_SPAD_LEAF_M22_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M22(i)     (22u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M23_BASE   23u
#define WIFI_VN_SPAD_LEAF_M23_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M23(i)     (23u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M3_BASE   3u
#define WIFI_VN_SPAD_LEAF_M3_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M3(i)     (3u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M4_BASE   4u
#define WIFI_VN_SPAD_LEAF_M4_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M4(i)     (4u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M5_BASE   5u
#define WIFI_VN_SPAD_LEAF_M5_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M5(i)     (5u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M6_BASE   6u
#define WIFI_VN_SPAD_LEAF_M6_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M6(i)     (6u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M7_BASE   7u
#define WIFI_VN_SPAD_LEAF_M7_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M7(i)     (7u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M8_BASE   8u
#define WIFI_VN_SPAD_LEAF_M8_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M8(i)     (8u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_M9_BASE   9u
#define WIFI_VN_SPAD_LEAF_M9_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_M9(i)     (9u + 32u * (i))
#define WIFI_VN_SPAD_LEAF_ZERO_BASE   25u
#define WIFI_VN_SPAD_LEAF_ZERO_STRIDE 32u
#define WIFI_VN_SPAD_LEAF_ZERO(i)     (25u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O0_BASE   512u
#define WIFI_VN_SPAD_SINK_O0_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O0(i)     (512u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O1_BASE   513u
#define WIFI_VN_SPAD_SINK_O1_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O1(i)     (513u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O10_BASE   522u
#define WIFI_VN_SPAD_SINK_O10_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O10(i)     (522u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O11_BASE   523u
#define WIFI_VN_SPAD_SINK_O11_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O11(i)     (523u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O12_BASE   524u
#define WIFI_VN_SPAD_SINK_O12_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O12(i)     (524u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O13_BASE   525u
#define WIFI_VN_SPAD_SINK_O13_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O13(i)     (525u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O14_BASE   526u
#define WIFI_VN_SPAD_SINK_O14_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O14(i)     (526u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O15_BASE   527u
#define WIFI_VN_SPAD_SINK_O15_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O15(i)     (527u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O16_BASE   528u
#define WIFI_VN_SPAD_SINK_O16_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O16(i)     (528u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O17_BASE   529u
#define WIFI_VN_SPAD_SINK_O17_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O17(i)     (529u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O18_BASE   530u
#define WIFI_VN_SPAD_SINK_O18_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O18(i)     (530u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O19_BASE   531u
#define WIFI_VN_SPAD_SINK_O19_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O19(i)     (531u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O2_BASE   514u
#define WIFI_VN_SPAD_SINK_O2_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O2(i)     (514u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O20_BASE   532u
#define WIFI_VN_SPAD_SINK_O20_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O20(i)     (532u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O21_BASE   533u
#define WIFI_VN_SPAD_SINK_O21_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O21(i)     (533u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O22_BASE   534u
#define WIFI_VN_SPAD_SINK_O22_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O22(i)     (534u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O23_BASE   535u
#define WIFI_VN_SPAD_SINK_O23_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O23(i)     (535u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O3_BASE   515u
#define WIFI_VN_SPAD_SINK_O3_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O3(i)     (515u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O4_BASE   516u
#define WIFI_VN_SPAD_SINK_O4_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O4(i)     (516u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O5_BASE   517u
#define WIFI_VN_SPAD_SINK_O5_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O5(i)     (517u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O6_BASE   518u
#define WIFI_VN_SPAD_SINK_O6_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O6(i)     (518u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O7_BASE   519u
#define WIFI_VN_SPAD_SINK_O7_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O7(i)     (519u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O8_BASE   520u
#define WIFI_VN_SPAD_SINK_O8_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O8(i)     (520u + 32u * (i))
#define WIFI_VN_SPAD_SINK_O9_BASE   521u
#define WIFI_VN_SPAD_SINK_O9_STRIDE 32u
#define WIFI_VN_SPAD_SINK_O9(i)     (521u + 32u * (i))
#define WIFI_VN_SPAD_SINK_SG_BASE   536u
#define WIFI_VN_SPAD_SINK_SG_STRIDE 32u
#define WIFI_VN_SPAD_SINK_SG(i)     (536u + 32u * (i))

#endif /* WIFI_VN_LAYOUT_H */
