PYTHON ?= python3
CXX ?= clang++

.PHONY: all test unit integration examples sanitize install clean check

all: test

test: unit integration

unit:
	$(PYTHON) -m unittest discover -s tests/unit -v

integration:
	CXX=$(CXX) $(PYTHON) tests/run_tests.py

# Run the whole suite with ASan/UBSan (detection layer for C++-side UB).
sanitize:
	CXXFLAGS="-fsanitize=address,undefined -fno-sanitize-recover=all -g" \
		CXX=$(CXX) $(PYTHON) tests/run_tests.py

# Compile and run every example program.
examples:
	@set -e; for f in examples/*.ep; do \
		echo "=== $$f"; \
		CXX=$(CXX) $(PYTHON) -m epitaxy --run -q "$$f"; \
	done

install:
	$(PYTHON) -m pip install .

clean:
	rm -rf build dist *.egg-info
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete
