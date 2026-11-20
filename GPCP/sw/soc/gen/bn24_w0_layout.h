/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: bn24_w0；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef BN24_W0_LAYOUT_H
#define BN24_W0_LAYOUT_H

#define BN24_W0_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define BN24_W0_LAYOUT_NPES        16u
#define BN24_W0_LAYOUT_WORDS       32u
#define BN24_W0_LAYOUT_BANKS       32u

#define BN24_W0_SPAD_LEAF_LLR_BASE   768u
#define BN24_W0_SPAD_LEAF_LLR_STRIDE 1u
#define BN24_W0_SPAD_LEAF_LLR(i)     (768u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M0_BASE   0u
#define BN24_W0_SPAD_LEAF_M0_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M0(i)     (0u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M1_BASE   32u
#define BN24_W0_SPAD_LEAF_M1_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M1(i)     (32u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M10_BASE   320u
#define BN24_W0_SPAD_LEAF_M10_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M10(i)     (320u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M11_BASE   352u
#define BN24_W0_SPAD_LEAF_M11_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M11(i)     (352u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M12_BASE   384u
#define BN24_W0_SPAD_LEAF_M12_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M12(i)     (384u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M13_BASE   416u
#define BN24_W0_SPAD_LEAF_M13_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M13(i)     (416u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M14_BASE   448u
#define BN24_W0_SPAD_LEAF_M14_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M14(i)     (448u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M15_BASE   480u
#define BN24_W0_SPAD_LEAF_M15_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M15(i)     (480u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M16_BASE   512u
#define BN24_W0_SPAD_LEAF_M16_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M16(i)     (512u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M17_BASE   544u
#define BN24_W0_SPAD_LEAF_M17_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M17(i)     (544u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M18_BASE   576u
#define BN24_W0_SPAD_LEAF_M18_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M18(i)     (576u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M19_BASE   608u
#define BN24_W0_SPAD_LEAF_M19_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M19(i)     (608u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M2_BASE   64u
#define BN24_W0_SPAD_LEAF_M2_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M2(i)     (64u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M20_BASE   640u
#define BN24_W0_SPAD_LEAF_M20_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M20(i)     (640u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M21_BASE   672u
#define BN24_W0_SPAD_LEAF_M21_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M21(i)     (672u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M22_BASE   704u
#define BN24_W0_SPAD_LEAF_M22_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M22(i)     (704u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M23_BASE   736u
#define BN24_W0_SPAD_LEAF_M23_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M23(i)     (736u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M3_BASE   96u
#define BN24_W0_SPAD_LEAF_M3_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M3(i)     (96u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M4_BASE   128u
#define BN24_W0_SPAD_LEAF_M4_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M4(i)     (128u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M5_BASE   160u
#define BN24_W0_SPAD_LEAF_M5_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M5(i)     (160u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M6_BASE   192u
#define BN24_W0_SPAD_LEAF_M6_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M6(i)     (192u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M7_BASE   224u
#define BN24_W0_SPAD_LEAF_M7_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M7(i)     (224u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M8_BASE   256u
#define BN24_W0_SPAD_LEAF_M8_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M8(i)     (256u + 1u * (i))
#define BN24_W0_SPAD_LEAF_M9_BASE   288u
#define BN24_W0_SPAD_LEAF_M9_STRIDE 1u
#define BN24_W0_SPAD_LEAF_M9(i)     (288u + 1u * (i))
#define BN24_W0_SPAD_SINK_HARD_BASE   800u
#define BN24_W0_SPAD_SINK_HARD_STRIDE 1u
#define BN24_W0_SPAD_SINK_HARD(i)     (800u + 1u * (i))
#define BN24_W0_SPAD_SINK_O0_BASE   0u
#define BN24_W0_SPAD_SINK_O0_STRIDE 1u
#define BN24_W0_SPAD_SINK_O0(i)     (0u + 1u * (i))
#define BN24_W0_SPAD_SINK_O1_BASE   32u
#define BN24_W0_SPAD_SINK_O1_STRIDE 1u
#define BN24_W0_SPAD_SINK_O1(i)     (32u + 1u * (i))
#define BN24_W0_SPAD_SINK_O10_BASE   320u
#define BN24_W0_SPAD_SINK_O10_STRIDE 1u
#define BN24_W0_SPAD_SINK_O10(i)     (320u + 1u * (i))
#define BN24_W0_SPAD_SINK_O11_BASE   352u
#define BN24_W0_SPAD_SINK_O11_STRIDE 1u
#define BN24_W0_SPAD_SINK_O11(i)     (352u + 1u * (i))
#define BN24_W0_SPAD_SINK_O12_BASE   384u
#define BN24_W0_SPAD_SINK_O12_STRIDE 1u
#define BN24_W0_SPAD_SINK_O12(i)     (384u + 1u * (i))
#define BN24_W0_SPAD_SINK_O13_BASE   416u
#define BN24_W0_SPAD_SINK_O13_STRIDE 1u
#define BN24_W0_SPAD_SINK_O13(i)     (416u + 1u * (i))
#define BN24_W0_SPAD_SINK_O14_BASE   448u
#define BN24_W0_SPAD_SINK_O14_STRIDE 1u
#define BN24_W0_SPAD_SINK_O14(i)     (448u + 1u * (i))
#define BN24_W0_SPAD_SINK_O15_BASE   480u
#define BN24_W0_SPAD_SINK_O15_STRIDE 1u
#define BN24_W0_SPAD_SINK_O15(i)     (480u + 1u * (i))
#define BN24_W0_SPAD_SINK_O16_BASE   512u
#define BN24_W0_SPAD_SINK_O16_STRIDE 1u
#define BN24_W0_SPAD_SINK_O16(i)     (512u + 1u * (i))
#define BN24_W0_SPAD_SINK_O17_BASE   544u
#define BN24_W0_SPAD_SINK_O17_STRIDE 1u
#define BN24_W0_SPAD_SINK_O17(i)     (544u + 1u * (i))
#define BN24_W0_SPAD_SINK_O18_BASE   576u
#define BN24_W0_SPAD_SINK_O18_STRIDE 1u
#define BN24_W0_SPAD_SINK_O18(i)     (576u + 1u * (i))
#define BN24_W0_SPAD_SINK_O19_BASE   608u
#define BN24_W0_SPAD_SINK_O19_STRIDE 1u
#define BN24_W0_SPAD_SINK_O19(i)     (608u + 1u * (i))
#define BN24_W0_SPAD_SINK_O2_BASE   64u
#define BN24_W0_SPAD_SINK_O2_STRIDE 1u
#define BN24_W0_SPAD_SINK_O2(i)     (64u + 1u * (i))
#define BN24_W0_SPAD_SINK_O20_BASE   640u
#define BN24_W0_SPAD_SINK_O20_STRIDE 1u
#define BN24_W0_SPAD_SINK_O20(i)     (640u + 1u * (i))
#define BN24_W0_SPAD_SINK_O21_BASE   672u
#define BN24_W0_SPAD_SINK_O21_STRIDE 1u
#define BN24_W0_SPAD_SINK_O21(i)     (672u + 1u * (i))
#define BN24_W0_SPAD_SINK_O22_BASE   704u
#define BN24_W0_SPAD_SINK_O22_STRIDE 1u
#define BN24_W0_SPAD_SINK_O22(i)     (704u + 1u * (i))
#define BN24_W0_SPAD_SINK_O23_BASE   736u
#define BN24_W0_SPAD_SINK_O23_STRIDE 1u
#define BN24_W0_SPAD_SINK_O23(i)     (736u + 1u * (i))
#define BN24_W0_SPAD_SINK_O3_BASE   96u
#define BN24_W0_SPAD_SINK_O3_STRIDE 1u
#define BN24_W0_SPAD_SINK_O3(i)     (96u + 1u * (i))
#define BN24_W0_SPAD_SINK_O4_BASE   128u
#define BN24_W0_SPAD_SINK_O4_STRIDE 1u
#define BN24_W0_SPAD_SINK_O4(i)     (128u + 1u * (i))
#define BN24_W0_SPAD_SINK_O5_BASE   160u
#define BN24_W0_SPAD_SINK_O5_STRIDE 1u
#define BN24_W0_SPAD_SINK_O5(i)     (160u + 1u * (i))
#define BN24_W0_SPAD_SINK_O6_BASE   192u
#define BN24_W0_SPAD_SINK_O6_STRIDE 1u
#define BN24_W0_SPAD_SINK_O6(i)     (192u + 1u * (i))
#define BN24_W0_SPAD_SINK_O7_BASE   224u
#define BN24_W0_SPAD_SINK_O7_STRIDE 1u
#define BN24_W0_SPAD_SINK_O7(i)     (224u + 1u * (i))
#define BN24_W0_SPAD_SINK_O8_BASE   256u
#define BN24_W0_SPAD_SINK_O8_STRIDE 1u
#define BN24_W0_SPAD_SINK_O8(i)     (256u + 1u * (i))
#define BN24_W0_SPAD_SINK_O9_BASE   288u
#define BN24_W0_SPAD_SINK_O9_STRIDE 1u
#define BN24_W0_SPAD_SINK_O9(i)     (288u + 1u * (i))

#endif /* BN24_W0_LAYOUT_H */
