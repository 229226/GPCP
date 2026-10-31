# B3 artifacts: device tree, user-space CGRA test, initramfs

ROOT    := ../..
BUILD   := $(ROOT)/build/linux
CROSS   := riscv64-linux-gnu-
BB      := $(ROOT)/third_party/busybox-1.36.1/busybox
INITRD  := $(BUILD)/initramfs

all: $(BUILD)/gpcp.dtb $(BUILD)/cgra_test $(BUILD)/initramfs.cpio

$(BUILD):
	mkdir -p $@

$(BUILD)/cgra_test: cgra_u.c | $(BUILD)
	env -u LD_LIBRARY_PATH $(CROSS)gcc -static -O2 -Wall -o $@ $<

$(BUILD)/gpcp.dtb: gpcp.dts | $(BUILD)
	dtc -I dts -O dtb -o $@ $<

$(BUILD)/initramfs.cpio: init $(BUILD)/cgra_test $(BB)
	rm -rf $(INITRD)
	mkdir -p $(INITRD)/bin $(INITRD)/dev $(INITRD)/proc $(INITRD)/sys $(INITRD)/tmp
	cp $(BB) $(INITRD)/bin/busybox
	for a in sh mount umount cat echo ls mkdir head grep poweroff reboot uname sleep; do \
		ln -sf busybox $(INITRD)/bin/$$a; \
	done
	cp init $(INITRD)/init
	chmod +x $(INITRD)/init
	cp $(BUILD)/cgra_test $(INITRD)/bin/cgra_test
	chmod +x $(INITRD)/bin/cgra_test
	cd $(INITRD) && find . -print0 | cpio --null -o -H newc > ../initramfs.cpio
	@ls -lh $(BUILD)/initramfs.cpio

clean:
	rm -rf $(BUILD)

.PHONY: all clean
