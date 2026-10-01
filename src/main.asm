;==============================================================================
;  Z80 3D DEMO  -  SEGA Master System, VDP in TMS9918A "Graphics II" mode
;
;  CPU : Z80A 3.58MHz            VDP : TMS9918A compatible mode 2 (256x192)
;
;  * 128x128 pixel 1bpp viewport, drawn into a RAM frame buffer, then copied
;    to the hidden VRAM page. Pages are flipped in vblank by switching the
;    name table (VDP register 2) -> no tearing.
;  * render modes: wireframe / hidden-line (back-face culling) / flat shaded
;    polygons (ordered-dither shading, light source fixed in view space)
;  * objects: cube, pyramid, octahedron, icosahedron, dodecahedron
;
;  Controls (pad 1):  button 1 = next mode,  button 2 = next object,
;                     up/down/left/right = change rotation speed
;  Without input the demo cycles modes/objects automatically.
;==============================================================================

;------------------------------------------------------------------ hardware
VDP_DATA    equ 0xBE
VDP_CTRL    equ 0xBF
VDP_VCOUNT  equ 0x7E
PORT_JOY1   equ 0xDC

JOY_UP      equ 0x01
JOY_DOWN    equ 0x02
JOY_LEFT    equ 0x04
JOY_RIGHT   equ 0x08
JOY_B1      equ 0x10
JOY_B2      equ 0x20

;------------------------------------------------------------------ VRAM map
; Mode 2: 3 screen thirds, each with its own 256 patterns / colors.
;   pattern generator 0x0000-0x17FF, color table 0x2000-0x37FF
;   name table A 0x3800, name table B 0x3C00, sprite attributes 0x3F00
; Viewport = tile columns 8..23, tile rows 4..19 (128x128 pixels).
;   page A uses patterns   0..127 of every third (third 0/2: 0..63 only)
;   page B uses patterns 128..255
;   third 0/2 free tiles: 64..127 = font (ASCII 0x20..0x5F), 255 = blank
VRAM_PAT    equ 0x0000
VRAM_COL    equ 0x2000
NT_A        equ 0x3800
NT_B        equ 0x3C00
VRAM_SAT    equ 0x3F00
REG2_A      equ NT_A / 0x400
REG2_B      equ NT_B / 0x400
BLANK_TILE  equ 255
FONT_TILE   equ 64          ; tile of ASCII 0x20

;------------------------------------------------------------------ 3D params
CENTER      equ 64          ; viewport centre (pixels)
ZOFF        equ 160         ; camera distance (must match tools/gen_tables.py)
AUTO_TIME   equ 160         ; rendered frames before auto-advancing

;------------------------------------------------------------------ RAM map
BUF         equ 0xC000      ; 2048 bytes: 128x128x1bpp, VRAM tile order
EDGE_L      equ 0xC800      ; 128 bytes: left x per scanline (polygon fill)
EDGE_R      equ 0xC880      ; 128 bytes: right x per scanline
SX          equ 0xC960      ; 32 bytes: projected x per vertex
SY          equ 0xC980      ; 32 bytes: projected y per vertex  (SY = SX+0x20)

VARS        equ 0xCA00
ang_x       equ VARS+0
ang_y       equ VARS+1
ang_z       equ VARS+2
spd_x       equ VARS+3
spd_y       equ VARS+4
spd_z       equ VARS+5
page        equ VARS+6      ; 0: page A visible, 1: page B visible
mode        equ VARS+7      ; 0 wire, 1 hidden line, 2 shaded
objno       equ VARS+8
joy_prev    equ VARS+9
idle        equ VARS+10
mat         equ VARS+12     ; 3x3 rotation matrix, signed, scale 127
lobj        equ VARS+21     ; light vector in object space (3 bytes)
sin_a       equ VARS+24
cos_a       equ VARS+25
sin_b       equ VARS+26
cos_b       equ VARS+27
sin_c       equ VARS+28
cos_c       equ VARS+29
t1          equ VARS+30
t2          equ VARS+31
obj_nv      equ VARS+32
obj_vtx     equ VARS+33     ; word
obj_ne      equ VARS+35
obj_edge    equ VARS+36     ; word
obj_nf      equ VARS+38
obj_face    equ VARS+39     ; word
sp_save     equ VARS+41     ; word
vptr        equ VARS+43     ; word
vidx        equ VARS+45
vx_t        equ VARS+46
vy_t        equ VARS+47
vz_t        equ VARS+48
rx_t        equ VARS+49
ry_t        equ VARS+50
f_t         equ VARS+51
ln_dx       equ VARS+52
ln_dy       equ VARS+53
fv_dx1      equ VARS+54
fv_dy1      equ VARS+55
fv_dx2      equ VARS+56
fv_dy2      equ VARS+57
face_cnt    equ VARS+58
first_v     equ VARS+59
ymin        equ VARS+60
ymax        equ VARS+61
pat_ptr     equ VARS+63     ; word
se_x0       equ VARS+65
se_cnt      equ VARS+66
se_sign     equ VARS+67
bb_c0       equ VARS+70     ; bounding box of this frame in tiles:
bb_c1       equ VARS+71     ;   columns c0..c1, rows r0..r1
bb_r0       equ VARS+72
bb_r1       equ VARS+73
page_bb     equ VARS+74     ; 2 x 4 bytes: what each VRAM page contains
clr_r0      equ VARS+82     ; tile rows of the RAM buffer that are dirty
clr_r1      equ VARS+83
blt_c0      equ VARS+84     ; area to copy to VRAM
blt_c1      equ VARS+85
blt_r0      equ VARS+86
blt_r1      equ VARS+87
blt_hi      equ VARS+88     ; VRAM page offset (high byte)
VARS_SIZE   equ 96

;==============================================================================
;  reset / interrupt vectors
;==============================================================================
    org 0x0000
    di
    im 1
    ld sp,0xDFF0
    jp start

    org 0x0038
irq_handler:                ; frame interrupt: only used to wake up from HALT
    push af
    in a,(VDP_CTRL)         ; acknowledge
    pop af
    reti

    org 0x0066
nmi_handler:                ; pause button: ignored
    retn

;==============================================================================
;  start-up
;==============================================================================
start:
    ; clear variables
    ld hl,VARS
    ld de,VARS+1
    ld bc,VARS_SIZE-1
    ld (hl),0
    ldir

    ; VDP registers
    in a,(VDP_CTRL)         ; reset control port latch
    ld hl,vdp_regs
    ld b,11
    ld c,0x80
.reg:
    ld a,(hl)
    out (VDP_CTRL),a
    ld a,c
    out (VDP_CTRL),a
    inc hl
    inc c
    djnz .reg

    ; clear all 16KB VRAM
    ld hl,0x0000
    call vram_set_write
    ld bc,0x4000
.clr:
    xor a
    out (VDP_DATA),a
    dec bc
    ld a,b
    or c
    jr nz,.clr

    ; no sprites
    ld hl,VRAM_SAT
    call vram_set_write
    ld a,0xD0
    out (VDP_DATA),a

    ; font patterns in third 0 and third 2
    ld hl,VRAM_PAT+FONT_TILE*8
    call vram_set_write
    ld hl,font_data
    ld bc,512
    call vram_copy
    ld hl,VRAM_PAT+0x1000+FONT_TILE*8
    call vram_set_write
    ld hl,font_data
    ld bc,512
    call vram_copy

    ; font colors: third 0 yellow, third 2 cyan
    ld hl,VRAM_COL+FONT_TILE*8
    ld bc,512
    ld d,0xB1
    call vram_fill
    ld hl,VRAM_COL+0x1000+FONT_TILE*8
    ld bc,512
    ld d,0x71
    call vram_fill

    ; name tables for both pages
    ld hl,NT_A
    call vram_set_write
    ld e,0
    call write_nametable
    ld hl,NT_B
    call vram_set_write
    ld e,128
    call write_nametable

    ; static texts
    ld hl,txt_title
    ld de,1*32+4
    call print_at
    ld hl,txt_sub
    ld de,2*32+7
    call print_at
    ld hl,txt_mode
    ld de,21*32+3
    call print_at
    ld hl,txt_obj
    ld de,22*32+3
    call print_at
    ld hl,txt_help
    ld de,23*32+5
    call print_at

    ; initial state
    ld a,2
    ld (spd_x),a
    ld a,3
    ld (spd_y),a
    ld a,1
    ld (spd_z),a
    ld a,0xFF
    ld (joy_prev),a
    ld a,15                 ; RAM buffer content unknown: clear all of it
    ld (clr_r1),a
    ld hl,page_bb           ; both VRAM pages empty: box (15,0,15,0)
    ld b,2
.pb:
    ld (hl),15
    inc hl
    ld (hl),0
    inc hl
    ld (hl),15
    inc hl
    ld (hl),0
    inc hl
    djnz .pb
    call apply_mode
    call apply_object

    ; display on, frame interrupt enabled (CPU keeps DI except while waiting)
    ld a,0xE0
    out (VDP_CTRL),a
    ld a,0x81
    out (VDP_CTRL),a

;==============================================================================
;  main loop
;==============================================================================
main_loop:
    call read_input
    call update_angles
    call build_matrix
    call transform
    call bounding_box

    call clear_buffer
    ld a,(mode)
    or a
    jr nz,.faces
    call draw_wire
    jr .drawn
.faces:
    call draw_faces
.drawn:
    ; tile (0,4) of the viewport doubles as the blank border tile of the
    ; middle screen third -> keep it empty (objects never reach it anyway)
    ld hl,BUF+512
    ld b,8
    xor a
.z: ld (hl),a
    inc hl
    djnz .z

    call blit
    call flip
    jp main_loop

;==============================================================================
;  input / state
;==============================================================================
read_input:
    in a,(PORT_JOY1)
    ld c,a                  ; current (active low)
    ld a,(joy_prev)
    ld b,a
    ld a,c
    ld (joy_prev),a
    cpl
    and b                   ; bits newly pressed
    ld b,a

    ; idle counter / auto demo
    or a
    jr z,.noinput
    ld a,0
    ld (idle),a
    jr .handle
.noinput:
    ld a,(idle)
    inc a
    ld (idle),a
    cp AUTO_TIME
    jr c,.handle
    xor a
    ld (idle),a
    ; auto: next mode, after the last mode also the next object
    ld a,(mode)
    cp 2
    push af
    call next_mode
    pop af
    call z,next_object
    ret

.handle:
    bit 4,b
    call nz,next_mode
    bit 5,b
    call nz,next_object
    ld hl,spd_x
    bit 0,b
    jr z,.nu
    dec (hl)
.nu:
    bit 1,b
    jr z,.nd
    inc (hl)
.nd:
    ld hl,spd_y
    bit 2,b
    jr z,.nl
    dec (hl)
.nl:
    bit 3,b
    jr z,.nr
    inc (hl)
.nr:
    ret

next_mode:                  ; preserves B
    push bc
    ld a,(mode)
    inc a
    cp 3
    jr c,.s
    xor a
.s: ld (mode),a
    call apply_mode
    pop bc
    ret

next_object:                ; preserves B
    push bc
    ld a,(objno)
    inc a
    cp NUM_OBJECTS
    jr c,.s
    xor a
.s: ld (objno),a
    call apply_object
    pop bc
    ret

apply_mode:
    ld a,(mode)
    ld hl,mode_colors
    call ptr_from_table
    call set_view_colors
    ld a,(mode)
    ld hl,mode_names
    call ptr_from_table
    ld de,21*32+10
    jp print_at

apply_object:
    ld a,(objno)
    ld hl,object_table
    call ptr_from_table     ; HL -> object header
    ld e,(hl)
    inc hl
    ld d,(hl)               ; DE -> name
    inc hl
    ld a,(hl)
    ld (obj_nv),a
    inc hl
    ld c,(hl)
    inc hl
    ld b,(hl)
    ld (obj_vtx),bc
    inc hl
    ld a,(hl)
    ld (obj_ne),a
    inc hl
    ld c,(hl)
    inc hl
    ld b,(hl)
    ld (obj_edge),bc
    inc hl
    ld a,(hl)
    ld (obj_nf),a
    inc hl
    ld c,(hl)
    inc hl
    ld b,(hl)
    ld (obj_face),bc
    ex de,hl
    ld de,22*32+10
    jp print_at

; HL = table of words, A = index -> HL = table[A]
ptr_from_table:
    add a,a
    ld e,a
    ld d,0
    add hl,de
    ld a,(hl)
    inc hl
    ld h,(hl)
    ld l,a
    ret

update_angles:
    ld hl,ang_x
    ld de,spd_x
    ld b,3
.l: ld a,(de)
    add a,(hl)
    ld (hl),a
    inc hl
    inc de
    djnz .l
    ret

;==============================================================================
;  math
;==============================================================================

; HL = A * E (signed 8x8) using quarter squares:
;   a*b = sq(a+b) - sq(a-b),  sq(n) = n*n/4
; With a' = a+128, b' = b+128:  a'+b' = a+b+256 (9 bit index into sqs),
; a'-b' = a-b (9 bit two's complement, borrow selects the upper half of sqd).
; Clobbers A, D, E.  ~140 cycles.
smul:
    xor 0x80
    ld d,a                  ; a'
    ld a,e
    xor 0x80
    ld e,a                  ; b'
    add a,d
    ld l,a
    ld a,sqs_lo/256
    adc a,0
    ld h,a                  ; HL -> sqs_lo[a'+b']
    ld a,d
    ld d,(hl)
    inc h
    inc h
    sub e                   ; a'-b', carry = negative
    ld e,(hl)               ; DE = sq(a+b)
    ld l,a
    ld a,sqd_lo/256
    adc a,0
    ld h,a                  ; HL -> sqd_lo[a-b]
    ld a,d
    sub (hl)
    ld d,a
    inc h
    inc h
    ld a,e
    sbc a,(hl)
    ld h,a
    ld l,d
    ret

neg_hl:
    xor a
    sub l
    ld l,a
    sbc a,a
    sub h
    ld h,a
    ret

; A = (HL + 64) >> 7, clamped to -127..127. Clobbers DE.
hl_to_a7:
    ld de,64
    add hl,de
    ld a,h
    add hl,hl
    xor h                   ; bit 7 = bit15 ^ bit14 -> overflow
    ld a,h
    ret p
    ld a,0x7F
    bit 7,h
    ret nz
    ld a,0x81
    ret

; A = (HL + 32) >> 6 (no overflow check: used for |HL| < 8000)
hl_to_a6:
    ld de,32
    add hl,de
    add hl,hl
    add hl,hl
    ld a,h
    ret

; A = (A * E) >> 7, signed
fmul:
    call smul
    jp hl_to_a7

; HL = HL / C (unsigned, C < 128). Clobbers A, B.
div16_8:
    xor a
    ld b,16
.l: add hl,hl
    rla
    cp c
    jr c,.s
    sub c
    inc l
.s: djnz .l
    ret

; A = angle -> C = sin, B = cos (signed, scale 127), so that
; "ld (sin_x),bc" stores sin_x, cos_x
sincos:
    ld h,sin_table/256
    ld l,a
    ld c,(hl)
    add a,64
    ld l,a
    ld b,(hl)
    ret

; Rotation matrix  M = Rz(c) * Ry(b) * Rx(a)
;   m00 = cb*cc   m01 = sa*sb*cc - ca*sc   m02 = ca*sb*cc + sa*sc
;   m10 = cb*sc   m11 = sa*sb*sc + ca*cc   m12 = ca*sb*sc - sa*cc
;   m20 = -sb     m21 = sa*cb              m22 = ca*cb
build_matrix:
    ld a,(ang_x)
    call sincos
    ld (sin_a),bc           ; sin_a, cos_a
    ld a,(ang_y)
    call sincos
    ld (sin_b),bc
    ld a,(ang_z)
    call sincos
    ld (sin_c),bc

    ld a,(sin_b)            ; t1 = sa*sb, t2 = ca*sb
    ld e,a
    ld a,(sin_a)
    call fmul
    ld (t1),a
    ld a,(sin_b)
    ld e,a
    ld a,(cos_a)
    call fmul
    ld (t2),a

    ld a,(cos_c)            ; m00
    ld e,a
    ld a,(cos_b)
    call fmul
    ld (mat+0),a

    ld a,(cos_c)            ; m01 = t1*cc - ca*sc
    ld e,a
    ld a,(t1)
    call smul
    push hl
    ld a,(sin_c)
    ld e,a
    ld a,(cos_a)
    call smul
    ex de,hl
    pop hl
    or a
    sbc hl,de
    call hl_to_a7
    ld (mat+1),a

    ld a,(cos_c)            ; m02 = t2*cc + sa*sc
    ld e,a
    ld a,(t2)
    call smul
    push hl
    ld a,(sin_c)
    ld e,a
    ld a,(sin_a)
    call smul
    pop de
    add hl,de
    call hl_to_a7
    ld (mat+2),a

    ld a,(sin_c)            ; m10
    ld e,a
    ld a,(cos_b)
    call fmul
    ld (mat+3),a

    ld a,(sin_c)            ; m11 = t1*sc + ca*cc
    ld e,a
    ld a,(t1)
    call smul
    push hl
    ld a,(cos_c)
    ld e,a
    ld a,(cos_a)
    call smul
    pop de
    add hl,de
    call hl_to_a7
    ld (mat+4),a

    ld a,(sin_c)            ; m12 = t2*sc - sa*cc
    ld e,a
    ld a,(t2)
    call smul
    push hl
    ld a,(cos_c)
    ld e,a
    ld a,(sin_a)
    call smul
    ex de,hl
    pop hl
    or a
    sbc hl,de
    call hl_to_a7
    ld (mat+5),a

    ld a,(sin_b)            ; m20
    neg
    ld (mat+6),a

    ld a,(cos_b)            ; m21
    ld e,a
    ld a,(sin_a)
    call fmul
    ld (mat+7),a

    ld a,(cos_b)            ; m22
    ld e,a
    ld a,(cos_a)
    call fmul
    ld (mat+8),a

    ; light direction (view space, from upper left front)
    ; l = (-2, 2, -1)/4  ->  object space: lobj = M^T l   (|lobj| <= 96)
    ld ix,mat
    ld iy,lobj
    ld b,3
.light:
    ld a,(ix+0)
    sra a
    neg
    ld c,a
    ld a,(ix+3)
    sra a
    add a,c
    ld c,a
    ld a,(ix+6)
    sra a
    sra a
    neg
    add a,c
    ld (iy+0),a
    inc ix
    inc iy
    djnz .light
    ret

; A = (row . v) >> 7,  IY -> matrix row, v = (vx_t, vy_t, vz_t)
dot_row:
    ld a,(vx_t)
    ld e,(iy+0)
    call smul
    push hl
    ld a,(vy_t)
    ld e,(iy+1)
    call smul
    pop de
    add hl,de
    push hl
    ld a,(vz_t)
    ld e,(iy+2)
    call smul
    pop de
    add hl,de
    jp hl_to_a7

; signed A (offset from centre) -> 0..127
clamp_screen:
    xor 0x80
    cp 64
    jr nc,.a
    ld a,64
.a: cp 192
    jr c,.b
    ld a,191
.b: sub 64
    ret

; rotate + project all vertices -> SX[], SY[]
transform:
    ld hl,(obj_vtx)
    ld (vptr),hl
    xor a
    ld (vidx),a
.vertex:
    ld hl,(vptr)
    ld a,(hl)
    ld (vx_t),a
    inc hl
    ld a,(hl)
    ld (vy_t),a
    inc hl
    ld a,(hl)
    ld (vz_t),a
    inc hl
    ld (vptr),hl

    ld iy,mat
    call dot_row
    ld (rx_t),a
    ld iy,mat+3
    call dot_row
    ld (ry_t),a
    ld iy,mat+6
    call dot_row
    add a,ZOFF              ; z' = z + ZOFF (always 100..220)
    ld l,a
    ld h,recip_table/256
    ld a,(hl)
    ld (f_t),a

    ld e,a                  ; sx = 64 + rx*f/64
    ld a,(rx_t)
    call smul
    call hl_to_a6
    call clamp_screen
    ld hl,vidx
    ld l,(hl)
    ld h,SX/256
    ld de,SX&0xFF
    add hl,de
    ld (hl),a

    ld a,(f_t)              ; sy = 64 - ry*f/64
    ld e,a
    ld a,(ry_t)
    call smul
    call hl_to_a6
    neg
    call clamp_screen
    ld hl,vidx
    ld l,(hl)
    ld h,SY/256
    ld de,SY&0xFF
    add hl,de
    ld (hl),a

    ld hl,vidx
    inc (hl)
    ld a,(obj_nv)
    cp (hl)
    jr nz,.vertex
    ret

;==============================================================================
;  frame buffer
;
;  RAM layout identical to the VRAM tiles:  addr = BUF + (y>>3)*128 +
;  (x>>3)*8 + (y&7),  pixel bit = 0x80 >> (x&7)
;==============================================================================

; clear the tile rows clr_r0..clr_r1 of the RAM buffer (last frame's
; drawing), then mark this frame's rows as dirty
clear_buffer:
    ld a,(clr_r0)
    ld b,a
    ld a,(clr_r1)
    sub b
    jr c,.done
    inc a
    add a,a
    ld c,a                  ; 2 x 32 pushes per tile row
    ld a,(clr_r1)
    inc a                   ; SP = BUF + (r1+1)*128
    ld l,0
    srl a
    rr l
    add a,BUF/256
    ld h,a
    ld (sp_save),sp
    ld sp,hl
    ld hl,0
    ld b,c
.l:
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    push hl
    djnz .l
    ld sp,(sp_save)
.done:
    ld a,(bb_r0)
    ld (clr_r0),a
    ld a,(bb_r1)
    ld (clr_r1),a
    ret

; bounding box of the projected vertices in tile units -> bb_c0..bb_r1
bounding_box:
    ld hl,SX
    call .minmax
    ld (bb_c0),de           ; E -> bb_c0, D -> bb_c1
    ld hl,SY
    call .minmax
    ld (bb_r0),de
    ret
.minmax:                    ; HL -> coordinates: E = min/8, D = max/8
    ld a,(obj_nv)
    ld b,a
    ld de,0x00FF
.l: ld a,(hl)
    cp e
    jr nc,.a
    ld e,a
.a: cp d
    jr c,.b
    ld d,a
.b: inc l
    djnz .l
    srl e
    srl e
    srl e
    srl d
    srl d
    srl d
    ret

; A = y -> HL = address of (0,y). Clobbers E.
row_addr:
    ld e,a
    rrca
    rrca
    rrca
    rrca
    and 7
    add a,BUF/256
    ld h,a
    ld a,e
    and 8
    add a,a
    add a,a
    add a,a
    add a,a
    ld l,a
    ld a,e
    and 7
    or l
    ld l,a
    ret

;------------------------------------------------------------------ lines

; A = vertex i, E = vertex j : draw line between projected vertices
line_idx:
    ld h,SX/256
    add a,SX&0xFF
    ld l,a
    ld b,(hl)
    add a,SY-SX
    ld l,a
    ld c,(hl)
    ld a,e
    add a,SX&0xFF
    ld l,a
    ld d,(hl)
    add a,SY-SX
    ld l,a
    ld e,(hl)
    ; fall through

; Bresenham line (B,C) -> (D,E), coordinates 0..127
draw_line:
    ld a,d
    sub b
    jr nc,.ordered
    ld a,b                  ; make x0 <= x1
    ld b,d
    ld d,a
    ld a,c
    ld c,e
    ld e,a
    ld a,d
    sub b
.ordered:
    ld (ln_dx),a
    ld a,e
    sub c
    ld (ln_dy),a
    ; start address / mask
    ld a,c
    call row_addr
    ld a,b
    and 0x78
    or l
    ld l,a
    ld a,b
    and 7
    or (mask_table&0xFF)+16
    ld e,a
    ld d,mask_table/256
    ld a,(de)
    ld c,a                  ; C = pixel mask

    ld a,(ln_dy)
    or a
    jp m,.up
    ld d,a                  ; D = |dy|
    ld a,(ln_dx)
    ld e,a                  ; E = dx
    cp d
    jr c,.ydown
    inc a
    ld b,a                  ; x-major, going down
    ld a,e
    srl a
    ex af,af'
    jp xmaj_down
.ydown:
    ld a,d                  ; y-major, going down
    inc a
    ld b,a
    ld a,d
    srl a
    ld d,e                  ; D = minor (dx), E = major (dy)
    ld e,b
    dec e
    ex af,af'
    jp ymaj_down
.up:
    neg
    ld d,a
    ld a,(ln_dx)
    ld e,a
    cp d
    jr c,.yup
    inc a
    ld b,a                  ; x-major, going up
    ld a,e
    srl a
    ex af,af'
    jp xmaj_up
.yup:
    ld a,d                  ; y-major, going up
    inc a
    ld b,a
    ld a,d
    srl a
    ld d,e
    ld e,b
    dec e
    ex af,af'
    jp ymaj_up

; Line loops.  HL = address, C = mask, B = pixel count,
;              D = minor delta, E = major delta, A' = error term
xmaj_down:
.l: ld a,c
    or (hl)
    ld (hl),a
    rrc c                   ; x+1
    jr nc,.x
    ld a,l
    add a,8
    ld l,a
.x: ex af,af'
    sub d
    jr nc,.noy
    add a,e
    ex af,af'
    inc l                   ; y+1
    ld a,l
    and 7
    jr nz,.n
    ld a,l
    add a,120
    ld l,a
    jr nc,.n
    inc h
    jr .n
.noy:
    ex af,af'
.n: djnz .l
    ret

xmaj_up:
.l: ld a,c
    or (hl)
    ld (hl),a
    rrc c                   ; x+1
    jr nc,.x
    ld a,l
    add a,8
    ld l,a
.x: ex af,af'
    sub d
    jr nc,.noy
    add a,e
    ex af,af'
    ld a,l                  ; y-1
    and 7
    jr z,.w
    dec l
    jr .n
.w: ld a,l
    sub 121
    ld l,a
    jr nc,.n
    dec h
    jr .n
.noy:
    ex af,af'
.n: djnz .l
    ret

ymaj_down:
.l: ld a,c
    or (hl)
    ld (hl),a
    inc l                   ; y+1
    ld a,l
    and 7
    jr nz,.y
    ld a,l
    add a,120
    ld l,a
    jr nc,.y
    inc h
.y: ex af,af'
    sub d
    jr nc,.nox
    add a,e
    ex af,af'
    rrc c                   ; x+1
    jr nc,.n
    ld a,l
    add a,8
    ld l,a
    jr .n
.nox:
    ex af,af'
.n: djnz .l
    ret

ymaj_up:
.l: ld a,c
    or (hl)
    ld (hl),a
    ld a,l                  ; y-1
    and 7
    jr z,.w
    dec l
    jr .y
.w: ld a,l
    sub 121
    ld l,a
    jr nc,.y
    dec h
.y: ex af,af'
    sub d
    jr nc,.nox
    add a,e
    ex af,af'
    rrc c                   ; x+1
    jr nc,.n
    ld a,l
    add a,8
    ld l,a
    jr .n
.nox:
    ex af,af'
.n: djnz .l
    ret

;------------------------------------------------------------------ wireframe

draw_wire:
    ld a,(obj_ne)
    ld b,a
    ld hl,(obj_edge)
.l: push bc
    ld a,(hl)
    inc hl
    ld e,(hl)
    inc hl
    push hl
    call line_idx
    pop hl
    pop bc
    djnz .l
    ret

;------------------------------------------------------------------ faces

; mode 1: outlines of visible faces, mode 2: shaded visible faces
draw_faces:
    ld ix,(obj_face)
    ld a,(obj_nf)
    ld (face_cnt),a
.l: call face_visible
    jr nc,.next
    ld a,(mode)
    cp 2
    jr z,.fill
    call outline_face
    jr .next
.fill:
    call shade_face
    call fill_face
.next:
    ld a,(ix+0)             ; skip n, indices, normal
    add a,4
    ld e,a
    ld d,0
    add ix,de
    ld hl,face_cnt
    dec (hl)
    jr nz,.l
    ret

; IX -> face. Carry set if facing the viewer (2D cross product < 0)
face_visible:
    ld h,SX/256
    ld a,(ix+1)
    add a,SX&0xFF
    ld l,a
    ld b,(hl)               ; x0
    add a,SY-SX
    ld l,a
    ld c,(hl)               ; y0
    ld a,(ix+2)
    add a,SX&0xFF
    ld l,a
    ld a,(hl)
    sub b
    ld (fv_dx1),a
    ld a,l
    add a,SY-SX
    ld l,a
    ld a,(hl)
    sub c
    ld (fv_dy1),a
    ld a,(ix+3)
    add a,SX&0xFF
    ld l,a
    ld a,(hl)
    sub b
    ld (fv_dx2),a
    ld a,l
    add a,SY-SX
    ld l,a
    ld a,(hl)
    sub c
    ld (fv_dy2),a
    ; cross = dx1*dy2 - dy1*dx2
    ld a,(fv_dy2)
    ld e,a
    ld a,(fv_dx1)
    call smul
    push hl
    ld a,(fv_dx2)
    ld e,a
    ld a,(fv_dy1)
    call smul
    ex de,hl
    pop hl
    or a
    sbc hl,de
    ld a,h
    rla                     ; carry = sign
    ret

; IX -> face: draw its outline
outline_face:
    push ix
    ld b,(ix+0)
    ld a,(ix+1)
    ld (first_v),a
.l: push bc
    ld e,(ix+2)
    dec b
    jr nz,.h
    ld a,(first_v)          ; closing edge
    ld e,a
.h: ld a,(ix+1)
    call line_idx
    pop bc
    inc ix
    djnz .l
    pop ix
    ret

; IX -> face: pat_ptr = dither pattern for light . normal
shade_face:
    push ix
    pop hl
    ld a,(ix+0)
    inc a
    ld e,a
    ld d,0
    add hl,de               ; HL -> normal
    ld a,(hl)
    inc hl
    push hl
    ld e,a
    ld a,(lobj+0)
    call smul
    ex (sp),hl              ; save partial sum, HL -> ny
    ld a,(hl)
    inc hl
    push hl
    ld e,a
    ld a,(lobj+1)
    call smul
    ex (sp),hl              ; HL -> nz, stack: sum2
    ld a,(hl)
    ld e,a
    ld a,(lobj+2)
    call smul
    pop de
    add hl,de
    pop de
    add hl,de               ; HL = S = lobj . n  (-6096..6096)
    ld de,6144
    add hl,de
    ld e,h                  ; 0..47
    ld d,0
    ld hl,level_table
    add hl,de
    ld e,(hl)               ; level*4
    ld hl,dither_table
    add hl,de
    ld (pat_ptr),hl
    ret

; IX -> face: scan convert into EDGE_L/EDGE_R, then fill the spans
fill_face:
    ld a,255
    ld (ymin),a
    xor a
    ld (ymax),a
    push ix
    ld b,(ix+0)
    ld a,(ix+1)
    ld (first_v),a
.e: push bc
    ld e,(ix+2)
    dec b
    jr nz,.h
    ld a,(first_v)
    ld e,a
.h: ld a,(ix+1)
    call scan_edge_idx
    pop bc
    inc ix
    djnz .e
    pop ix

    ; loop state lives in one register bank (HL = row address, B = rows
    ; left, C = y), the span code works in the other one (exx)
    ld a,(ymin)
    ld c,a
    call row_addr
    ld a,(ymax)
    sub c
    inc a
    ld b,a
.y: ld a,c
    inc c
    push hl
    exx
    ld l,a
    ld h,EDGE_L/256
    ld b,(hl)               ; xl
    set 7,l
    ld c,(hl)               ; xr
    and 3                   ; pattern byte for this line
    ld hl,(pat_ptr)
    add a,l
    ld l,a
    ld d,(hl)               ; (dither table does not cross a page)
    ld a,c
    cp b
    jr nc,.ok
    ld c,b
    ld b,a
.ok:
    pop hl
    call span
    exx
    inc l                   ; next row address
    ld a,l
    and 7
    jr nz,.s
    ld a,l
    add a,120
    ld l,a
    jr nc,.s
    inc h
.s: djnz .y
    ret

; edge from vertex A to vertex E
scan_edge_idx:
    ld h,SX/256
    add a,SX&0xFF
    ld l,a
    ld b,(hl)
    add a,SY-SX
    ld l,a
    ld c,(hl)
    ld a,e
    add a,SX&0xFF
    ld l,a
    ld d,(hl)
    add a,SY-SX
    ld l,a
    ld e,(hl)
    ; ymin / ymax (every vertex starts one edge)
    ld a,(ymin)
    cp c
    jr c,.nm
    ld a,c
    ld (ymin),a
.nm:
    ld a,(ymax)
    cp c
    jr nc,.nx
    ld a,c
    ld (ymax),a
.nx:
    ld a,e
    sub c
    ret z                   ; horizontal edge
    jr c,.right
    ld hl,EDGE_L            ; going down -> left side
    jr scan_edge
.right:                     ; going up -> right side (scan it top-down)
    ld a,b
    ld b,d
    ld d,a
    ld a,c
    ld c,e
    ld e,a
    ld hl,EDGE_R
    ; fall through

; B=x0 C=y0 D=x1 E=y1 (y1 > y0), HL = edge table
scan_edge:
    ld a,l
    add a,c
    ld l,a
    push hl                 ; -> table[y0]
    ld a,b
    ld (se_x0),a
    ld a,e
    sub c
    ld (se_cnt),a
    ld c,a                  ; C = dy
    ld a,d
    sub b                   ; dx (signed)
    ld (se_sign),a
    jp p,.pos
    neg
.pos:
    ld h,a
    ld l,0
    call div16_8            ; HL = |dx|*256/dy
    ld a,(se_sign)
    or a
    call m,neg_hl
    ld b,h
    ld c,l                  ; BC = step (8.8)
    ld a,(se_x0)
    ld d,a
    ld e,0x80               ; DE = x (8.8), rounded
    ld a,(se_cnt)
    inc a
    pop hl
.l: ld (hl),d
    inc l
    ex de,hl
    add hl,bc
    ex de,hl
    dec a
    jr nz,.l
    ret

; fill span B..C (inclusive) on the row at HL with pattern D. Clobbers A, E.
span:
    ld a,b
    and 0x78
    ld e,a
    or l
    ld l,a                  ; HL -> first byte
    ld a,c
    and 0x78
    sub e
    rrca
    rrca
    rrca
    ld e,a                  ; E = number of tile steps
    push hl
    ld h,mask_table/256
    ld a,b
    and 7
    or (mask_table&0xFF)
    ld l,a
    ld b,(hl)               ; left mask
    ld a,c
    and 7
    or (mask_table&0xFF)+8
    ld l,a
    ld c,(hl)               ; right mask
    pop hl
    ld a,e
    or a
    jr nz,.multi
    ld a,b
    and c
    ld b,a
    ld a,(hl)               ; single byte: new = old ^ ((old ^ pat) & mask)
    xor d
    and b
    xor (hl)
    ld (hl),a
    ret
.multi:
    ld a,(hl)
    xor d
    and b
    xor (hl)
    ld (hl),a
    ld a,l
    add a,8
    ld l,a
    dec e
    jr z,.right
.mid:
    ld (hl),d
    ld a,l
    add a,8
    ld l,a
    dec e
    jr nz,.mid
.right:
    ld a,(hl)
    xor d
    and c
    xor (hl)
    ld (hl),a
    ret

;==============================================================================
;  VRAM transfer / page flip
;==============================================================================

; Copy the RAM frame buffer to the hidden page. Only the tiles inside
; (this frame's box) U (box of what the hidden page currently shows) are sent.
blit:
    ld a,(page)
    xor 1
    rlca
    rlca
    ld (blt_hi),a           ; 0x00 page A, 0x04 page B (+128 tiles)
    ld e,a
    ld d,0
    ld hl,page_bb
    add hl,de               ; HL -> box of the hidden page
    ld de,bb_c0
    ld bc,blt_c0
    call .min               ; c0
    call .max               ; c1
    call .min               ; r0
    call .max               ; r1

    ld a,(blt_r0)
.row:
    push af
    ld l,a                  ; VRAM address = row base + page + c0*8
    ld h,0
    add hl,hl
    ld de,vram_rows
    add hl,de
    ld e,(hl)
    inc hl
    ld d,(hl)
    ld a,(blt_c0)
    add a,a
    add a,a
    add a,a
    ld c,a                  ; C = c0*8
    or e
    ld l,a
    ld a,(blt_hi)
    add a,d
    ld h,a
    call vram_set_write
    pop af
    push af
    ld l,0                  ; RAM address = BUF + row*128 + c0*8
    srl a
    rr l
    add a,BUF/256
    ld h,a
    ld a,l
    or c
    ld l,a
    ld a,(blt_c0)
    ld b,a
    ld a,(blt_c1)
    sub b
    inc a
    add a,a
    add a,a
    add a,a
    ld b,a                  ; bytes = (c1-c0+1)*8
    ld c,VDP_DATA
.o: outi                    ; 30 cycles per byte: safe during active display
    nop
    jp nz,.o
    pop af
    ld hl,blt_r1
    cp (hl)
    ret nc
    inc a
    jr .row

; (BC) = min((DE),(HL)); (HL) = (DE); advance all three
.min:
    ld a,(de)
    cp (hl)
    jr c,.m1
    ld a,(hl)
.m1:
    ld (bc),a
    jr .adv
.max:
    ld a,(de)
    cp (hl)
    jr nc,.m2
    ld a,(hl)
.m2:
    ld (bc),a
.adv:
    ld a,(de)
    ld (hl),a
    inc hl
    inc de
    inc bc
    ret

; wait for vblank and show the page that was just drawn
flip:
    in a,(VDP_VCOUNT)
    cp 0xC0
    jr nc,.now              ; already in vblank
    in a,(VDP_CTRL)         ; drop stale frame interrupt
    ei
    halt
    di
.now:
    ld a,(page)
    xor 1
    ld (page),a
    add a,REG2_A
    out (VDP_CTRL),a
    ld a,0x82
    out (VDP_CTRL),a
    ret

;==============================================================================
;  VRAM helpers
;==============================================================================

; HL = VRAM address (write)
vram_set_write:
    ld a,l
    out (VDP_CTRL),a
    ld a,h
    or 0x40
    out (VDP_CTRL),a
    ret

; copy BC bytes from HL to VRAM
vram_copy:
    ld a,(hl)
    out (VDP_DATA),a
    inc hl
    dec bc
    ld a,b
    or c
    jr nz,vram_copy
    ret

; fill BC bytes at VRAM HL with D
vram_fill:
    call vram_set_write
.l: ld a,d
    out (VDP_DATA),a
    dec bc
    ld a,b
    or c
    jr nz,.l
    ret

; Name table: E = tile offset of the page (0 or 128). VRAM address already set.
write_nametable:
    ld d,0                  ; row
.row:
    ld c,BLANK_TILE         ; border tile of this row
    ld a,d
    cp 8
    jr c,.b
    cp 16
    jr nc,.b
    ld c,e                  ; middle third: viewport tile 0 (kept blank)
.b: ld l,0                  ; L = 1 if this row is inside the viewport
    ld a,d
    cp 4
    jr c,.cols
    cp 20
    jr nc,.cols
    inc l
    ld a,d
    and 0x18                ; rows 4..7 -> local row = row - 4
    ld a,d
    jr nz,.nl
    sub 4
.nl:
    and 7
    add a,a
    add a,a
    add a,a
    add a,a
    add a,e
    ld h,a                  ; first tile of this row
.cols:
    ld b,0
.col:
    ld a,l
    or a
    jr z,.border
    ld a,b
    sub 8
    jr c,.border
    cp 16
    jr nc,.border
    add a,h
    jr .out
.border:
    ld a,c
.out:
    out (VDP_DATA),a
    inc b
    ld a,b
    cp 32
    jr nz,.col
    inc d
    ld a,d
    cp 24
    jr nz,.row
    ret

; HL = 16 band colors (one per viewport tile row) -> color table of both pages
set_view_colors:
    ld d,0                  ; band
.band:
    ld a,(hl)
    inc hl
    push hl
    ld c,a                  ; color
    ld a,d
    cp 4
    jr c,.t0
    cp 12
    jr c,.t1
    sub 12
    ld e,0x10
    jr .go
.t1:
    sub 4
    ld e,0x08
    jr .go
.t0:
    ld e,0
.go:
    ld l,0                  ; address = VRAM_COL + third*0x800 + local*128
    srl a
    rr l
    add a,e
    add a,VRAM_COL/256
    ld h,a
    push hl
    call vram_set_write     ; page A
    call .write128
    pop hl
    ld a,h
    add a,4
    ld h,a
    call vram_set_write     ; page B
    call .write128
    pop hl
    inc d
    ld a,d
    cp 16
    jr nz,.band
    ret
.write128:
    ld b,128
    ld a,c
.w: out (VDP_DATA),a
    nop
    djnz .w
    ret

; HL = zero terminated string, DE = name table offset. Written to both pages.
print_at:
    push hl
    ld hl,NT_A
    add hl,de
    call vram_set_write
    pop hl
    push hl
    call .emit
    ld hl,NT_B
    add hl,de
    call vram_set_write
    pop hl
.emit:
    ld a,(hl)
    or a
    ret z
    add a,FONT_TILE-0x20
    out (VDP_DATA),a
    inc hl
    jr .emit

;==============================================================================
;  data
;==============================================================================
vdp_regs:
    db 0x02                 ; R0: M3 = Graphics II
    db 0x80                 ; R1: 16K, display off (enabled later)
    db REG2_A               ; R2: name table 0x3800
    db 0xFF                 ; R3: color table 0x2000 (full)
    db 0x03                 ; R4: pattern table 0x0000 (full)
    db VRAM_SAT/0x80        ; R5: sprite attributes 0x3F00
    db 0x03                 ; R6: sprite patterns 0x1800
    db 0x01                 ; R7: backdrop black
    db 0x00                 ; R8: (SMS) h-scroll
    db 0x00                 ; R9: (SMS) v-scroll
    db 0xFF                 ; R10: (SMS) line interrupt off

; VRAM pattern address of each viewport tile row (page A)
vram_rows:
    dw 0x0000,0x0080,0x0100,0x0180                      ; third 0
    dw 0x0800,0x0880,0x0900,0x0980,0x0A00,0x0A80,0x0B00,0x0B80  ; third 1
    dw 0x1000,0x1080,0x1100,0x1180                      ; third 2

txt_title:  db "Z80A + TMS9918A 3D DEMO",0
txt_sub:    db "SEGA MASTER SYSTEM",0
txt_mode:   db "MODE:",0
txt_obj:    db "OBJ :",0
txt_help:   db "1:MODE 2:OBJ PAD:SPIN",0

mode_names:
    dw .w, .h, .s
.w: db "WIREFRAME   ",0
.h: db "HIDDEN LINE ",0
.s: db "FLAT SHADED ",0

; color table value per viewport band (fg << 4 | bg)
mode_colors:
    dw .w, .h, .s
.w: db 0xF1,0xF1,0x71,0x71,0x71,0x71,0x71,0x71,0x51,0x51,0x51,0x51,0x51,0x51,0x41,0x41
.h: db 0xB1,0xB1,0xB1,0xB1,0xB1,0xB1,0xA1,0xA1,0xA1,0xA1,0x91,0x91,0x91,0x91,0x81,0x81
.s: db 0xF1,0xF1,0xF1,0xF1,0xF1,0xF1,0xF1,0xF1,0xF1,0xF1,0xF1,0xF1,0xF1,0xF1,0xF1,0xF1

    include "gen_font.inc"
    include "gen_objects.inc"

; page aligned tables
    align 256
    include "gen_tables.inc"

    assert (mask_table & 31) == 0
    assert (dither_table & 0xFF) + 68 <= 256

;==============================================================================
;  SEGA header (checksum is filled in by tools/smsheader.py)
;==============================================================================
    org 0x7FF0
    db "TMR SEGA"
    db 0x00,0x00            ; reserved
    dw 0x0000               ; checksum
    db 0x00,0x00            ; product code
    db 0x00                 ; version
    db 0x4C                 ; region: SMS export, ROM size 32KB
