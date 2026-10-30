/* GPCP SoC - bare-metal startup (RV64) */
.section .text.start, "ax"
.global _start

_start:
    la      t0, trap
    csrw    mtvec, t0
    la      sp, _stack_top

    /* zero .bss */
    la      t0, _bss_start
    la      t1, _bss_end
1:
    bgeu    t0, t1, 2f
    sb      zero, 0(t0)
    addi    t0, t0, 1
    j       1b
2:
    call    main

    /* normal completion */
    li      t0, 0x40010000
    li      t1, 0x600D
    sw      t1, 0(t0)
3:
    j       3b

trap:
    li      t0, 0x40010000
    li      t1, 0xDEAD
    sw      t1, 0(t0)
4:
    j       4b
