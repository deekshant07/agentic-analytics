# Analytics Agent

Natural-language analytics over DuckDB. Ask questions in plain English; get charts, SQL, and narrative. Built with Streamlit.

## Run locally

```bash
uv sync
cp .env.example .env   # add your LLM API key
uv run streamlit run chat.py
```

## Share a public link (Streamlit Community Cloud)

1. Push this repo to GitHub (see below).
2. Go to [share.streamlit.io](https://share.streamlit.io) → **Create app**.
3. Repository: your fork, **Branch**: `main`, **Main file path**: `chat.py`.
4. **Advanced settings** → Python: `3.12` (app supports 3.12+; local dev may use 3.13 via `uv`).
5. **Secrets** (TOML format):

```toml
LLM_PROVIDER = "groq"
GROQ_API_KEY = "gsk_..."
```

6. Deploy. Share the `*.streamlit.app` URL.

### Feedback

Each assistant reply has thumbs up/down and an optional note. Feedback is stored in `core/chat_history.db` on the server (per deployment instance).

## Data

- `jupiter.duckdb` — demo events + users (Jan–May 22, 2026).
- Trim future-dated events: `uv run python scripts/trim_events_after_date.py --after 2026-05-22`

## Tests

```bash
pytest qa/regression.py -m "not db" -q
```
