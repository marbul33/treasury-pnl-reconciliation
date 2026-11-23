PYTHON := python

.PHONY: data reconcile test report all clean

data:
	$(PYTHON) src/01_generate_data.py

reconcile:
	$(PYTHON) src/02_reconcile.py

test:
	pytest

report:
	quarto render report/index.qmd

all: data reconcile test report

clean:
	rm -rf data/raw/*.csv data/raw/*.json data/mart/* report/index.html report/*_files