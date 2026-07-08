.PHONY: run run-bot test test-harden test-telegram clean lint

PYTHON = python3

run:
	$(PYTHON) main.py

run-test:
	TEST_MODE=true $(PYTHON) main.py

test:
	$(PYTHON) -m pytest tests/ -v --tb=short -x

test-harden:
	$(PYTHON) -m pytest tests/test_hardening.py -v --tb=short

test-telegram:
	$(PYTHON) -m pytest tests/test_telegram.py -v --tb=short

clean:
	rm -rf data/ logs/ __pycache__ */__pycache__ .pytest_cache
	rm -f .shutdown_requested
