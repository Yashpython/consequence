.PHONY: up down reset test test-live

up:
	docker compose up -d
	docker compose exec -T postgres sh -c 'until pg_isready -U consequence -d consequence; do sleep 0.5; done'

down:
	docker compose down

reset:
	python -c "from consequence.environment import reset; reset()"

test:
	pytest

test-live:
	pytest -m live
