# GPCP backend adapter targets.  Does not touch the top-level build.
PY := python3
ADAPTER := adapter.py
RUNNER := run.py
BUNDLE ?= /home/zzq/wlbench/build/bundles/nr_mimo
BACKEND ?= gpcp-cgra

.PHONY: regress collect evaluate run capabilities ci verify-bundle clean

regress:
	$(PY) $(ADAPTER) regress

collect:
	$(PY) $(ADAPTER) collect --scenario nr_mimo --backend gpcp-cgra \
		--out build/metrics_cgra.json

evaluate:
	$(PY) $(ADAPTER) evaluate --scenario nr_mimo --backend gpcp-cgra \
		--out build/eval_cgra.json

capabilities:
	$(PY) $(ADAPTER) capabilities

# 一个后端的完整自检：冻结回归 + 全 bundle 能力报告 + 真跑一个 bundle。
ci: regress capabilities
	$(PY) $(RUNNER) --bundle $(BUNDLE) --backend $(BACKEND) \
		--out build/metrics_ci.json
	$(PY) $(ADAPTER) evaluate --scenario $(shell basename $(BUNDLE)) \
		--backend $(BACKEND)

verify-bundle:
	$(PY) $(ADAPTER) verify-bundle --scenario nr_mimo

run:
	$(PY) $(RUNNER) --bundle $(BUNDLE) --backend $(BACKEND) \
		--out build/metrics_$(BACKEND).json

clean:
	rm -rf build __pycache__
