# pet-consult 常用命令
.PHONY: install test test-unit test-integration test-e2e lint run smoke bench start-all status-all stop-all

install:
	pip install -e ".[dev]"

test:
	python -m pytest

test-unit:
	python -m pytest tests/unit

test-integration:
	python -m pytest tests/integration

test-e2e:
	python -m pytest tests/e2e

lint:
	python -m ruff check app tests

run:
	uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8081 --reload

smoke:
	python scripts/smoke_consult.py

bench:
	python scripts/benchmark.py

start-all:
	bash scripts/start_all.sh

status-all:
	bash scripts/status_all.sh

stop-all:
	bash scripts/stop_all.sh
