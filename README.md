# Trading Bot

A paper-trading bot and platform for US stocks/ETFs and crypto. It uses **simulated money only**, and its design is inspired by NexusTrade.

- Investment approach: [docs/STRATEGY.md](docs/STRATEGY.md)
- Roadmap: [docs/ROADMAP.md](docs/ROADMAP.md)
- Dev process and agent org: [CLAUDE.md](CLAUDE.md), [.claude/agents/](.claude/agents/)

```bash
uv sync
uv run pytest
```

## Dev console (local)

A read-only Streamlit app for looking at the project between build waves: progress, price charts, the strategies in plain English, a cost sandbox, and review reports. It shows generated practice data by default and never trades or changes anything.

```bash
uv run streamlit run devconsole/app.py
```

Then open http://localhost:8501 in your browser.

Not financial advice.
