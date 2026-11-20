# Requires GNU Make >= 4.0 for .RECIPEPREFIX.
.RECIPEPREFIX = >
.PHONY: install build verify conf test sweep demo ci clean

install:
> pip install -e . --no-build-isolation

build:
> python3 -m wlbench.cli build --all

verify:
> python3 -m wlbench.cli verify

conf:
> python3 -m wlbench.cli conformance

test:
> python3 -m unittest discover -s tests -v

sweep:
> python3 -m wlbench.cli sweep --scenario mmse_scalable --set layers=2,4,8 --set rx=2,4,8

demo:
> python3 examples/reference_backend.py --all

ci:
> bash scripts/ci.sh

clean:
> rm -rf build
