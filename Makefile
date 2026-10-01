# Z80 3D demo for SEGA Master System (TMS9918A mode 2)
PYTHON ?= python3
ROM    := build/z80_3d_demo.sms
SYM    := build/z80_3d_demo.sym

GEN := src/gen_tables.inc src/gen_objects.inc src/gen_font.inc

.PHONY: all clean gen test

all: $(ROM)

$(GEN): tools/gen_tables.py
	$(PYTHON) tools/gen_tables.py

gen: $(GEN)

$(ROM): src/main.asm $(GEN) tools/z80asm.py tools/smsheader.py
	@mkdir -p build
	$(PYTHON) tools/z80asm.py src/main.asm -o $@ --sym $(SYM) --size 0x8000
	$(PYTHON) tools/smsheader.py $@

# run headless in the test emulator, save screenshots to build/shots
test: $(ROM)
	$(PYTHON) tools/smsemu.py $(ROM) --sym $(SYM) --frames 1500 \
	    --shot 60,400,720,1000,1400 --outdir build/shots \
	    --input "300:b1,600:b1,900:b2,1200:b2"

clean:
	rm -rf build
