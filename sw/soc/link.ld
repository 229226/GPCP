OUTPUT_ARCH(riscv)
ENTRY(_start)

MEMORY {
  RAM (rwx) : ORIGIN = 0x80000000, LENGTH = 512K
}

SECTIONS {
  .text : {
    *(.text.start)
    *(.text*)
  } > RAM

  .rodata : { *(.rodata*) *(.srodata*) } > RAM
  .data   : { *(.data*)   *(.sdata*)   } > RAM

  .bss : {
    _bss_start = .;
    *(.bss*) *(.sbss*) *(COMMON)
    _bss_end = .;
  } > RAM

  /* 描述符环：跟着 .bss 走（4 KiB 对齐 + 一页间隙），但不低于 0x80020000。
   * .bss 一旦长过环基址，环自动上移，不会再被描述符覆盖。
   * 环窗口固定 32 KiB（RING_DEPTH*32B），因此不得越过 0x80040000 ——
   * ifft/ssb_tx 的 67 KB / 140 KB 结果区就放在这个地址，再往上才是 0x80070000 等。 */
  _ring_base = MAX(ALIGN(4096) + 4096, 0x80020000);
  ASSERT(_ring_base + 0x8000 <= 0x80040000,
         "descriptor ring window overlaps fixed buffers: shrink .bss or move buffers")

  _stack_top = ORIGIN(RAM) + LENGTH(RAM);
}
