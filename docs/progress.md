# Progress log

Read at the start of every session, updated at the end: what's done, what's next, open
questions. A phase ends only when its gate passes, with the evidence recorded here.

## Phases

| Phase | When | Gate before the next phase | Status |
| --- | --- | --- | --- |
| 0 · Setup | Week 1 · days 1–2 | CI is green, Damian can SSH in as `deploy`, root login is refused, and `docker run hello-world` works on the server (backups and alerts configured) | Open |
| 1 · Recorder live | Week 1 · days 2–4 | 24 h with no gap over 5 min in `option_quotes`; day-one volume measured and the raw-stream cap set from it | Open |
| 2 · Data and checks | Week 1 · days 4–7 | Restore from R2 works; the gap report covers every recorded day; the liquidity report names the book | Open |
| 2B · Rough end-to-end version | End of week 1 | Paper service runs 24 h and shows its trades; shortcut list exists (results don't count yet) | Open |
| 3 · Pricing and surface | Week 2 | IVs and Greeks match Deribit and finite differences within agreed tolerances; golden tests match btc-vol-models; live surface on the status page | Open |
| 4 · Backtest engine | Weeks 2–3 | Determinism, leakage and reconciliation tests pass on a real week; hand-checked smoke trade matches | Open |
| 5 · Strategies and results | Week 3 | Full report of v1 rules vs every benchmark on the locked test, after costs | Open |
| 6 · Streamlit dashboard | Weeks 3–4, in parallel | Public site loads every page on laptop and phone; nothing on it changes the system | Open |
| 7 · JEV challenger | Week 4 | JEV-versus-rules report on the same frozen folds, seeds and cost model | Open |
| 8 · Testnet paper trading | Weeks 3–4, runs a week | A week of paper decisions matches its replay, every difference explained | Open |
| 9 · Release v1.0 | End of week 4 | A fresh clone runs end to end with one command; history scan is clean | Open |
| 10 · Challenger models | Week 5, after v1.0 | Each challenger promoted or rejected on evidence | Open |

## Current phase

Phase 0 · Setup. In progress.

## Next

Phase 1 · Recorder live, once the Phase 0 gate passes.

## Open questions

- JEV details (inputs, outputs, determinism, speed and cost per call, training dates, whether it
  can be self-hosted). Needed by week 4.
