PYTHON ?= python3

.PHONY: setup check check-backend check-frontend check-public check-containers

setup:
	$(PYTHON) -m venv .venv
	.venv/bin/python -m pip install -r backend/requirements.txt

check:
	$(PYTHON) scripts/check.py

check-backend:
	$(PYTHON) scripts/check.py --suite backend

check-frontend:
	$(PYTHON) scripts/check.py --suite frontend

check-public:
	$(PYTHON) scripts/check.py --suite privacy

check-containers:
	$(PYTHON) scripts/check_containers.py
