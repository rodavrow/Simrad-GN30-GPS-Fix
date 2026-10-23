| GN30 on-target flash routine for Fujitsu MBM29F800BA (word mode) on MC68341 CS0.
| Runs from SRAM. Entered from BDM with GO; returns to BDM with BGND.
| In:  a2 = 0xAAA, a3 = 0x554 (word-mode unlock addresses as CPU byte addresses)
| Out: d7 = status (0 ok, 1 program fail, 2 erase fail), a1 = address reached / failing
        .text
        .globl  _start
_start:
| ---- entry +0: PROGRAM  a0 = source (RAM), a1 = flash dest, d0 = word count
prog:   move.w  (%a0)+,%d1
        cmp.w   #0xFFFF,%d1
        beq.s   pnext                   | erased value: nothing to program
        move.w  #0x00AA,(%a2)
        move.w  #0x0055,(%a3)
        move.w  #0x00A0,(%a2)
        move.w  %d1,(%a1)
ppoll:  move.w  (%a1),%d2
        cmp.w   %d1,%d2
        beq.s   pnext                   | data polling: reads back the data when done
        btst    #5,%d2                  | DQ5 = internal time limit exceeded
        beq.s   ppoll
        move.w  (%a1),%d2
        cmp.w   %d1,%d2
        beq.s   pnext
        move.w  #0x00F0,(%a1)           | reset to read mode
        moveq   #1,%d7
        bra.w   hloop
pnext:  addq.l  #2,%a1
        subq.l  #1,%d0
        bne.s   prog
        moveq   #0,%d7
        bra.w   hloop

| ---- ERASE  a1 = sector address
erase:  move.w  #0x00AA,(%a2)
        move.w  #0x0055,(%a3)
        move.w  #0x0080,(%a2)
        move.w  #0x00AA,(%a2)
        move.w  #0x0055,(%a3)
        move.w  #0x0030,(%a1)
epoll:  move.w  (%a1),%d2
        cmp.w   #0xFFFF,%d2
        beq.s   edone
        btst    #5,%d2
        beq.s   epoll
        move.w  (%a1),%d2
        cmp.w   #0xFFFF,%d2
        beq.s   edone
        move.w  #0x00F0,(%a1)
        moveq   #2,%d7
        bra.w   hloop
edone:  moveq   #0,%d7
        bra.w   hloop

| ---- ID  a1 = sector address -> d3 = manufacturer, d4 = device, d5 = protect status
ident:  move.w  #0x00AA,(%a2)
        move.w  #0x0055,(%a3)
        move.w  #0x0090,(%a2)
        moveq   #0,%d3
        moveq   #0,%d4
        moveq   #0,%d5
        move.w  0x0000.w,%d3
        move.w  0x0002.w,%d4
        move.w  4(%a1),%d5
        move.w  #0x00F0,0x0000.w        | exit autoselect (device is in ID mode, not program mode)
        moveq   #0,%d7
| ---- common exit. If the CPU is ever resumed by accident (spurious GO from a
| ---- BDM link error) it executes BGND again straight away.
hloop:  bgnd
        bra.w   hloop

        .globl  erase, ident
