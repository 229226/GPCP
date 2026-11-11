/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: wifi_cn24；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * kind: cn，规则调度器布局见 gpcp_layout.h 命名区域
 */
#ifndef WIFI_CN24_LAYOUT_H
#define WIFI_CN24_LAYOUT_H

#define WIFI_CN24_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define WIFI_CN24_LAYOUT_NPES        16u
#define WIFI_CN24_LAYOUT_WORDS       32u
#define WIFI_CN24_LAYOUT_BANKS       32u

/* 该 kernel 的布局由专用调度器管理；C 侧用 gpcp_layout.h 的几何宏。 */

#endif /* WIFI_CN24_LAYOUT_H */
