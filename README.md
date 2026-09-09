# consequence

A benchmark that grades LLM agents on final database state rather than on their own transcripts.

**Status: in development**

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Copy `.env.example` to `.env` and fill in the values you need:

```bash
cp .env.example .env
```

## Development

```bash
ruff check .
pytest
```
