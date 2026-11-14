# GPCP backend adapter targets.  Does not touch the top-level build.
PY := python3
ADAPTER := adapter.py

.PHONY: regress collect evaluate verify-bundle clean

regress:
	$(PY) $(ADAPTER) regress

collect:
	$(PY) $(ADAPTER) collect --scenario nr_mimo --backend gpcp-cgra \
		--out build/metrics_cgra.json

evaluate:
	$(PY) $(ADAPTER) evaluate --scenario nr_mimo --backend gpcp-cgra \
		--out build/eval_cgra.json

verify-bundle:
	$(PY) $(ADAPTER) verify-bundle --scenario nr_mimo

clean:
	rm -rf build __pycache__
