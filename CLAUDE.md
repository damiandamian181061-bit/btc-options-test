# CLAUDE.md: btc-options-lab

## Project

btc-options-lab is a research platform for BTC options on Deribit. Damian makes the finance
calls, approves each step and handles anything involving accounts, payments and keys. Claude
Code writes all the code and tests.

The platform records Deribit market data around the clock on a small server (minute snapshots in
PostgreSQL/TimescaleDB plus a raw top-of-book and trade stream in compressed Parquet), prices
options with Black-76 on a fitted volatility surface, and backtests a fixed-rule credit-spread
strategy (bull put and bear call spreads) against two benchmark sets. It then paper-trades that
strategy on Deribit testnet and shows the results on a public, read-only Streamlit dashboard.
The main book is BTC_USDC linear options, with inverse BTC options as the fallback. It is
**paper only**: there is no live trading and no live keys exist anywhere.

- Full brief: `docs/brief.md` (trading plan, rules, build plan, risks, prompts per phase).
- What's done and what's next: `docs/progress.md`.
- Ideas for later phases: `docs/later.md`.
- Server rebuild steps: `infra/server-setup.md`.

## Repo layout

One installable Python package (`src/optlab`), with the blueprint's twelve modules as folders.

```text
btc-options-lab/
  src/optlab/
    domain/  market_data/  pricing/  volatility/  forecasts/
    strategy/  selector/  risk/  simulator/  execution/  ledger/  evaluation/
    config.py     settings from env/.env; refuses to start if LIVE_TRADING=true
  apps/worker/  apps/dashboard/
  infra/      Docker Compose, migrations, server setup
  configs/    fees, risk limits, strategies, backtests
  tests/  research/  docs/
  CLAUDE.md
```

## Commands

Python 3.12, managed with uv.

```bash
uv sync                               # create .venv and install everything from uv.lock
uv run pytest                         # tests
uv run ruff check                     # lint
uv run ruff format                    # format
uv run mypy                           # type checks (strict for src/)
uv run pre-commit run --all-files     # all hooks, including the gitleaks secret scan
```

CI (`.github/workflows/ci.yml`) runs lint, format check, mypy, pytest, the file-hygiene hooks
and a gitleaks scan of the full history on every push.

## Rules for Claude Code

Each rule holds unless Damian changes it.

### Every session

- Start by reading `CLAUDE.md`, `docs/brief.md` and `docs/progress.md`, then state the task being
  taken and its gate.
- Work on one task from `docs/progress.md` at a time. Write the tests first for maths, accounting
  and settlement.
- Finish with every test passing, a clear commit, a push to GitHub, and an updated progress log:
  done, next, open questions.
- When the finance intent is unclear, ask Damian. Never guess a trading or accounting rule.

### Conventions

- Timestamps in UTC milliseconds; τ in years (ACT/365) to the 08:00 UTC expiry; IV as a decimal;
  log-moneyness k = ln(K/F).
- Every money value carries its currency: USDC for linear options, BTC for inverse option
  premiums. Size is in BTC contracts.
- Vega and theta use Deribit's units, checked against Deribit's own Greeks in Phase 3, so the two
  compare directly.
- The cash ledger uses exact decimals; pricing maths uses floating point.
- All market data is read through `MarketDataView(as_of=t)`, and nothing reads data after t.
- Fills happen at executable bid/ask within quoted size. Mark and mid appear only in runs labelled
  as comparisons.

### Safety

- `LIVE_TRADING` must be false. The program refuses to start otherwise, and no live keys ever
  exist.
- Never read, print or commit `.env` or any secret. Settings deny reading `.env`, and secret
  scanning runs before every commit and in CI.
- Explain every server command in plain words before running it. Never delete data, drop tables or
  wipe disks on the server without Damian's explicit yes and a verified backup.
- Back up before every database migration on the server.
- Never commit raw vendor data, and never put the server's address in the repo.

### Quality

- Every maths function has golden tests: btc-vol-models numbers, Deribit's values and finite
  differences.
- Ruff, mypy and pytest pass before every commit, and CI is green before merging.
- A phase ends only when its gate passes, with the evidence recorded in `docs/progress.md`.

### Scope

- New ideas go in `docs/later.md`, not into the current phase.
- Keep the module boundaries from the repo layout. Strategies, JEV and any AI never touch
  execution; only the risk gateway passes orders on.

## Working notes

- **Secrets.** Programs read secrets from `.env`, which Damian fills in by hand. `.claude/settings.json`
  denies reading `.env` and `.env.*`, and that includes `.env.example`. Its variable list is
  mirrored by the fields of `src/optlab/config.py`, and `tests/test_config.py` checks the two
  match.
- **Server.** `recorder-1` is a DigitalOcean Droplet in London (LON1), Ubuntu 26.04. Log in as
  `deploy` with Damian's SSH key; root login and passwords are refused. Damian gives the IP address
  in chat. Never write it, the R2 endpoint or the Cloudflare account ID into any file: use
  placeholders such as `SERVER_IP`. The repo lives at `/srv/btc-options-lab`, pulled with a
  read-only deploy key. The firewall is the DigitalOcean cloud firewall `ssh-only`; containers must
  not publish ports to the internet.
- **Commits.** Use Damian's GitHub no-reply address (set in this repo's local git config), never
  a personal email.
