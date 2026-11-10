# Progress log

Read at the start of every session, updated at the end: what's done, what's next, open
questions. A phase ends only when its gate passes, with the evidence recorded here.

## Phases

| Phase | When | Gate before the next phase | Status |
| --- | --- | --- | --- |
| 0 · Setup | Week 1 · days 1–2 | CI is green, Damian can SSH in as `deploy`, root login is refused, and `docker run hello-world` works on the server (backups and alerts configured) | **Passed** 10 Nov 2026 |
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

Phase 0 · Setup: **passed** on 10 November 2026. Phase 1 has not started.

## Phase 0 · Setup: gate evidence

Gate checked on 2026-11-10 00:21 UTC, with fresh SSH connections from the laptop:

| Check | Result |
| --- | --- |
| CI is green | GitHub Actions run 42725115101 on commit `0a42884`: **success** (both jobs: "Lint, types and tests" and "Secret scan (gitleaks, full history)") |
| Damian can SSH in as `deploy` | `ssh deploy@SERVER_IP` → `logged in as deploy on recorder-1` (key `damian-laptop`, SHA256:4ztd2bk0…) |
| Root login is refused | `ssh root@SERVER_IP` → `Permission denied (publickey).` |
| `docker run hello-world` works | As `deploy`, without sudo → `Hello from Docker!` |

Also verified, after the reboot (22:30 UTC on 9 Nov):

- **SSH settings in effect:** `permitrootlogin no`, `passwordauthentication no`,
  `kbdinteractiveauthentication no`, `authenticationmethods publickey`. A password-only login as
  deploy is refused with `Permission denied (publickey)`.
- **Automatic security updates:** unattended-upgrades active, `Update-Package-Lists "1"`,
  `Unattended-Upgrade "1"`.
- **Time:** time zone Etc/UTC, clock synchronized, chrony tracking a Canonical NTS server
  (12 µs off).
- **Docker:** 29.9.3 with Compose v5.7.0, active and enabled at boot; `json-file` logs capped at
  5 × 10 MB, compressed.
- **Swap:** 2 GB `/swapfile` in fstab, swappiness 10.
- **Updates:** all 42 installed; 0 waiting; no reboot flag; kernel 7.0.0-43.
- **Web Console agent:** `droplet-agent` active. ufw left off, as decided.
- **Repo on the server:** `/srv/btc-options-lab` cloned with the read-only deploy key "recorder-1
  (read-only)" (SHA256:qZv0igcj…). `.env` was copied from `.env.example`, mode 600, ignored by git,
  values blank.
- **Locally:** 36 tests pass; ruff, mypy (strict) and every pre-commit hook pass; a gitleaks scan
  of the full history found no leaks; the server IP appears in no repo file; Claude Code's read of
  `.env.example` was refused by the deny rule.
- **Backups and alerts:** the R2 bucket `btc-options-backup` with its key (28 Oct) and the three
  healthchecks.io checks (27 Oct) were set up by Damian. Their values go into the server's `.env`
  before Phases 1 and 2.

Decisions made during Phase 0 (9 Nov):

- `deploy` has passwordless sudo and is in the `docker` group.
- ufw stays off; the DigitalOcean cloud firewall `ssh-only` is the firewall.
- Updates were installed and the server rebooted at the end of hardening.
- The deploy key was created by Claude Code and added through `gh`.
- Docker is updated by hand (not automatic); no automatic reboots.

## Next

Phase 1 · Recorder live (Prompt 1). Before it starts, Damian pastes `HC_PING_URL_RECORDER` and
`HC_PING_URL_DISK` into `/srv/btc-options-lab/.env` on the server.

## Open questions

- JEV details (inputs, outputs, determinism, speed and cost per call, training dates, whether it
  can be self-hosted). Needed by week 4.
- The DigitalOcean metrics agent (`do-agent`) is not installed on recorder-1, although the brief
  says monitoring is on. Install it from the Droplet's Graphs tab if you want the graphs (see
  `docs/later.md`).
- The brief says API keys created for the old site are revoked before Phase 0 ends. Damian to
  confirm this is done (or that none exist).
