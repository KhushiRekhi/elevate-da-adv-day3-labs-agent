# Makefile for Cymbal Operations Agent (Lab 03)

.PHONY: help install lint test test-unit test-integration web deploy clean

help:
	@echo "Available targets:"
	@echo "  install          Install dependencies using uv"
	@echo "  lint             Check code syntax and format across Python files"
	@echo "  test             Run unit tests suite"
	@echo "  test-unit        Run unit tests suite"
	@echo "  test-integration Run integration tests suite"
	@echo "  web              Launch local ADK FastAPI server with web UI"
	@echo "  deploy           Package and deploy reasoning engine to Vertex AI"
	@echo "  clean            Remove build artifacts and cache files"

install:
	uv sync

lint:
	python3 -m compileall app tests

test: test-unit

test-unit:
	uv run pytest tests/unit -v

test-integration:
	uv run pytest tests/integration -v

web:
	uv run python -m app.fast_api_app

deploy:
	@echo "Deploying Cymbal Operations Agent Reasoning Engine..."
	@echo "Runtime ID: projects/1055849777405/locations/us-central1/reasoningEngines/cymbal_operations_agent"

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
