# GPCP top-level convenience targets

.PHONY: all test build clean sw-check

all: test

# Build the Verilator model (if needed) and run all CGRA kernel tests
test:
	python3 tools/cgra.py test --rebuild

build:
	python3 tools/cgra.py build

sw-check:
	$(MAKE) -C sw all
	$(MAKE) -C sw rv64

clean:
	rm -rf build sim/cgra/build
