# BTC/USDC Options Platform — Build Brief (Damian + Claude)

Oct 22, 2026 · @Damian

> **Snapshot copy.** Exported on 14 November 2026 from the live Claude Doc, which stays the source of truth: https://claude.ai/artifact/1FWZes94e2zuUMb6XKDER2
>
> The doc has two tabs, both included here: Part 1 is the Build Brief, Part 2 (at the end of this file) is the Claude Code prompts.

## Start here

You make the finance calls, approve what Claude Code does, and handle anything involving accounts, payments and keys. Claude Code writes all the code: data and engineering, data quality and analytics, and the code for your maths and finance. The target is about a month of full-day work, with the challenger models following in week 5.

**It won't be built in one go, and nothing is lost between sessions.**

- The build runs as many Claude Code sessions over about four weeks of full days. Each session takes the next task from the plan, writes it with tests, runs them, commits, and updates the progress log.
- Code, tests, docs and Git history stay on your laptop, GitHub and the server. Claude's memory of the conversation does not carry over, so the repo carries it instead.
- Every session starts by reading `CLAUDE.md` (the rules in this doc) and `docs/progress.md` (what's done, what's next).
- The recorder runs on the server around the clock, whether or not a session is open. Market data is lost only if the recorder stops, so it gets automatic restarts, alerts and backups.
- Small, tested steps are also what stop one mistake from building on another.

**Secrets never go in this doc.** That means passwords, API keys, secret keys, ping URLs and your SSH private key. This doc is a web page one share away from anyone, and it will end up in a public repo.

Claude Code doesn't need to see secrets either: the programs read them from a `.env` file that you fill in yourself. Keep each secret in a password manager. Non-secret details (server IP, usernames, bucket name, domain) go in the Details table.

**How this becomes Claude Code's brief.** Once setup is done and the open questions are answered, we export this doc into the repo. The Rules for Claude Code section becomes `CLAUDE.md` and the rest goes in `docs/`; from then on the repo is the source of truth.

Each phase starts from its own prompt in the Claude Code prompts tab (Part 2 of this file).

**Your to-do list, in order**

Now. These unblock the recorder, the one urgent part:

1. Choose the Claude plan and set up a password manager. Done: Bitwarden set up 26–27 October; staying on Pro for now (decided 9 November), upgrading if usage limits slow the build.
2. Install WSL, Docker Desktop, Claude Code and VS Code on your laptop. Done 26–27 October. On 9 November Claude Code was set to start in Manual mode, and the SSH key is loaded with keychain.
3. Create the GitHub repo, private until release. Done 27 October.
4. Create the server. Done 27 October, on DigitalOcean.
5. Create the Cloudflare R2 backup bucket and key. Done 28 October.
6. Create the healthchecks.io checks. Done 27 October.
7. Fill in the Details table. Done 9 November.

During week 1:

8. Make sure btc-vol-models and BTC-Options-Strategy- are in your GitHub account.
9. Answer the remaining open questions at the end.

Later:

10. Set up the domain and the Streamlit dashboard in Phase 6.
11. Send JEV's details before Phase 7.
12. Create the Deribit testnet key in Phase 8.

## Decisions so far

Every disagreement between the team plan and blueprint v1.1 is now settled, mostly by doing both. Your answers from 25 October are folded in below.

| Topic | Decision | Notes |
| --- | --- | --- |
| Team | You and Claude Code. Claude Code writes all code, including Omer's and Reggie's lanes and your maths. | You make the finance calls and approve each step. |
| Timeline | About a month of full-day work, then the challenger models in week 5. A rough end-to-end version runs by the end of week 1. | Week-by-week schedule in the Build plan. |
| Where we build | Claude Code in the Ubuntu (WSL) terminal on your laptop, with GitHub connected; VS Code for reading the code. Tests and backtests run on the laptop. | Same Linux as the server, so what works locally works there. |
| Always-on server | One small server (DigitalOcean Basic Droplet in London: 2 vCPU, 4 GB memory, 80 GB disk) for the recorder, paper trading and the dashboard. Created 27 October; Hetzner was skipped because it is refusing new customers. | A laptop sleeps, travels and restarts; these three can't stop. |
| Code | Starting from scratch. | No old code, handover or spec carried over. |
| Database | PostgreSQL with the TimescaleDB extension, so both in one. | Explained below. |
| Repo layout | Blueprint v1.1 module names, as one Python package. | Explained below. |
| What we record | Both: minute snapshots in the database, plus a raw top-of-book and trade stream in compressed files. | Raw volume is measured on day one, then capped. |
| Which books | BTC\_USDC linear options and inverse BTC options, plus perps, futures, index and delivery prices. | The inverse book is the fallback if the USDC book is thin. |
| Historical data | No purchase. Backtests use our own recording, which grows every day. | Results state exactly which period they cover. |
| Benchmarks | Both sets. Set A: no-trade, hold-to-expiry, take-profit/stop, volatility threshold, random valid selection. Set B: cash, BTC hold, long straddle, delta-hedged straddle, IV vs RV, calendar. | Set B means the engine also trades the perp for hedging. |
| Models | Black-76 first. GEV/EVT tail model with JEV in week 4. Heston and Bates in week 5, after release v1.0. | Everything else depends on Black-76. |
| Selector | Fixed rules are the champion; JEV is the challenger. | JEV only proposes; the risk gateway decides. You send JEV's details before Phase 7. |
| Spread orders | Combo orders in paper trading where Deribit offers them. Backtests fill each leg at its own bid/ask. | Leg-by-leg fills are the conservative test. |
| Valuation | Mark at Deribit's mark price. Forward from the matching future, else put-call parity. Spline smile first, SVI later. Settle at Deribit's delivery price. | Since 2026 an in-the-money option settles through its expiry's future, with the same payoff. See Settlement below. |
| Paper capital | 100,000 USDC | Raised from 50,000 on 25 October. |
| Ranking weights | α 97.5%, λ 1, κ 0, μ 0 to start | Confirmed. Tuned on validation data only. |
| Dashboard | Streamlit, public and read-only at damiantrades.com, or damian00trades.com if it's taken. | Nothing on the public site can change the system; admin actions run from your laptop. |
| Repo | `btc-options-test` (renamed from `btc-options-lab`), public; MIT licence at release. Old site linked in the README as v1. | Kept public by your decision on 17 November 2026. The server deploys from it. |
| Backups | Cloudflare R2, capped at about $10 a month, with a $5 budget alert; bucket and key created 28 October. Alerts by email from healthchecks.io, set up 27 October. |  |
| Claude plan | Pro for now (decided 9 November); upgrade to Max if usage limits slow the build. | See Costs. |
| Claude Code set-up | Opus 5.5 at xhigh effort, saved as the default. Manual permission mode by default; accept edits may be switched on for a session; no auto mode for now. SSH key loaded with keychain. | Decided 9 November. Auto mode to be reconsidered for the laptop-only coding from Phase 3. |
| Server access | Admin user deploy: key-only login, passwordless sudo. Root and password logins off. ufw off. This project's containers never publish ports; ports 80 and 443 are open only for your other project's dashboard, which shares this server. | Done 9 November. The second project was kept here by your decision on 17 November 2026. Rebuild steps in infra/server-setup.md. |
| Server upkeep | Security updates install automatically. When one needs a restart, the server restarts by itself at 04:00 UTC, and only then; Docker and every service start again on their own, and the gap is labelled server\_reboot. Docker is updated by hand. UTC time with chrony; 2 GB swap. DigitalOcean's metrics agent isn't installed. | Done 9 November; reboot policy decided 15 November 2026. On 17 November a restart brought every service back by itself, with one minute missed. |
| GitHub access | GitHub CLI (gh) from GitHub's own apt source, with Git over HTTPS. The server pulls the repo into /srv/btc-options-lab with a read-only deploy key. | Done 10 November. The deploy key was added through gh, so the laptop stays signed in to gh. |
| Server .env | You pasted the R2 and healthchecks values yourself (10 November). The database password was generated on the server and written in without ever being displayed (13 November). | Only the deploy user can read it. |
| Recorder data rules | IVs stored as decimals (Deribit sends percent). An empty bid or ask is stored as blank, never as 0. One point-in-time row per option per minute; each trade stored once. Instruments read from Deribit's metadata, never parsed from names. | Decided 12–13 November. |
| Phase 1 gate rule | Strict: no more than 5 minutes between any two recorded minutes across the 24 hours, with missing minutes at either end counting. | Decided 13 November. The query is infra/sql/gap\_check.sql. |
| Tests and deploys | Database tests must run in CI: without a test database they fail rather than skip. CI also builds the Docker image and checks it refuses LIVE\_TRADING=true. A pg\_dump backup before every database migration on the server. | Since 13 November. |
| Old-site API keys | Not reused. Any key this project needs is made fresh. | Decided 14 November. |
| Phase 2 decisions | BTC\_USDC linear is the main book; the inverse book keeps being recorded as the fallback. No entry when no BTC\_USDC expiry is 7 to 30 days away (no switch to inverse). Data-quality thresholds confirmed. Flag scope: an option's flag blocks only spreads using it; a flag on an expiry's future blocks that expiry; global flags block every entry. The minimum credit stays 20% of the width for v1. | Decided 15 November 2026. Alternatives to the minimum credit are tested in Phase 5 on validation data only. |

**Database: TimescaleDB is PostgreSQL with an add-on, so we get both.** PostgreSQL is the database; TimescaleDB is an extension installed inside it. Ordinary tables (instruments, orders, backtest results) stay plain PostgreSQL.

The big time-series tables (quotes, ticks) become TimescaleDB hypertables. These split data by time and compress older chunks heavily. It's still one database, one connection and standard SQL.

**Repo layout: blueprint names, one package.** The blueprint's twelve modules become folders inside one installable Python package, not twelve separate packages. You keep the clean boundaries without twelve sets of build files to maintain.

```text
btc-options-lab/
  src/optlab/
    domain/  market_data/  pricing/  volatility/  forecasts/
    strategy/  selector/  risk/  simulator/  execution/  ledger/  evaluation/
  apps/worker/  apps/dashboard/
  infra/      Docker Compose, migrations, server setup
  configs/    fees, risk limits, strategies, backtests
  tests/  research/  docs/
  CLAUDE.md
```

**Settlement: through the expiry's future, with the same payoff.** Since early April 2026, an in-the-money linear (USDC) option first becomes a position in its expiry's linear future, at the strike. That future then cash-settles in USDC at the delivery price, the time-weighted average of the Deribit index from 07:30 to 08:00 UTC. Inverse options have settled the same way since 1 August 2026.

For the backtest, each in-the-money leg is still paid its intrinsic value at the delivery price, and out-of-the-money legs expire worthless. The option's usual delivery fee is charged once, when it converts into the future; the rate comes from Deribit's fee page in Phase 2B. Netting against an expiry future already held can only lower fees, and v1 holds none.

Deribit lists a linear future for every linear option expiry, so each expiry in the USDC book has its own future for the forward.

Sources, checked 14 November 2026: [Deribit: Linear USDC Options](https://support.deribit.com/hc/en-us/articles/31424932728093-Linear-USDC-Options) · [Deribit: Inverse Options](https://support.deribit.com/hc/en-us/articles/31424939096093-Inverse-Options) · [Deribit: Futures Expiries](https://support.deribit.com/hc/en-us/articles/25944688876957-Contract-Introduction-Policy)

## Your setup steps

Steps 1 to 8 come now, in order, and take an evening or two. Steps 9 to 11 come later in the build.

**1. Password manager (10 minutes)**

- Install Bitwarden (free) or another password manager you trust, and turn on two-step login.
- Every key and password from the steps below goes here first, named as in the Details table.

**2. WSL: Linux inside Windows (20 minutes and a restart)**

- WSL runs a real Ubuntu Linux inside Windows. Claude Code, Docker and the project all live there, so your laptop behaves like the server.
- Open PowerShell as administrator (Start, type PowerShell, Run as administrator) and run `wsl --install`.
- Restart when asked. Ubuntu opens and asks for a Linux username and password; save both in the password manager.
- Your Core Ultra 5 226V has 16 GB of memory built in, and it can't be upgraded. Claude Code will cap WSL at about 8 GB so Windows stays responsive.

**3. Docker Desktop (20 minutes)**

- Docker runs each part of the platform (database, recorder, paper trader, dashboard) in its own sealed container. One command then starts the whole stack, identically on your laptop and the server.
- To check whether you have it: Start menu, type Docker. Or in PowerShell, run `docker --version`.
- If not, download Docker Desktop for Windows from docker.com and install it with the WSL 2 option ticked. It's free for personal use.
- In Docker Desktop: Settings, Resources, WSL integration, then turn on Ubuntu.
- Test it in the Ubuntu window: `docker run hello-world` should print a welcome message.

**4. Claude Code and VS Code (30 minutes)**

- Claude Code needs a paid Claude plan (Pro or Max); the free plan doesn't include it. Costs says which plan to pick.
- In the Ubuntu window, run the Linux installer from Anthropic's [setup guide](https://code.claude.com/docs/en/setup): `curl -fsSL https://claude.ai/install.sh | bash`
- Type `claude` and sign in with your Claude account. Always start it from the Ubuntu window, since that's where it's installed.

- VS Code is the editor where you read the code, review Claude Code's changes and see GitHub, all in one window.
- Install VS Code on Windows from [code.visualstudio.com](https://code.visualstudio.com/), then add Microsoft's WSL extension from its Extensions panel.
- In the Ubuntu window, go to the project folder and type `code .`; VS Code opens connected to Ubuntu.
- In VS Code's Extensions panel, search "Claude Code" and install the one published by Anthropic. It uses the same Claude plan and limits as the terminal.

**5. GitHub repo (10 minutes)**

- On github.com: Settings, Emails, tick "Keep my email addresses private". Commits then show a GitHub no-reply address, which matters once the repo is public.
- Create a new repository named `btc-options-lab` (or your choice), Private, with no README. Claude Code fills it in the first session and connects your laptop to GitHub with you.

**6. SSH key (5 minutes)**

- An SSH key pair is how your laptop proves who it is to the server. The private half never leaves your laptop; the public half is safe to share.
- In Ubuntu, run `ssh-keygen -t ed25519 -C "damian-laptop"`. Press Enter for the default location and set a passphrase (save it in the password manager).
- Run `cat ~/.ssh/id_ed25519.pub` and copy the whole line it prints for step 7.

**7. Server (30 minutes)**

A server is a computer you rent in a data centre. It stays on around the clock, which a laptop can't, and runs the recorder, the database and the public website. We rent ours from DigitalOcean, which calls its servers Droplets. Ours is a Basic Droplet with 2 processors, 4 GB of memory and an 80 GB disk. Everything else, including tests and backtests, runs on your laptop.

Done on 27 October with DigitalOcean. Hetzner was skipped: since 26 June 2026 it has been refusing new cloud servers to new customers. The steps, kept for a rebuild:

1. Sign up at [digitalocean.com](https://www.digitalocean.com/) with email. Turn on two-step login: profile icon, My Account, Two-factor authentication.
2. Create, Droplets: region London (LON1); image Ubuntu 26.04 (LTS) x64; size Basic, Regular CPU, $24 a month (2 vCPU, 4 GB RAM, 80 GB SSD, 4 TB transfer).
3. Authentication, SSH Keys, Add an SSH Key: paste your public key from step 6, name it `damian-laptop`, and tick it.
4. Public IPv4 on, IPv6 off, Improved Metrics monitoring (free) on, backups off; name `recorder-1`.
5. Networking, Firewalls, Create Firewall `ssh-only`: one inbound rule, SSH on TCP port 22; leave the outbound rules as they are; apply it to `recorder-1`.
6. Note the Droplet's public IPv4 address in your own notes. It isn't secret, but it stays out of this doc and the repo.

Test it: in Ubuntu, run `ssh root@YOUR_SERVER_IP`, type `yes` the first time, and you're in. Type `exit` to leave. From then on Claude Code sets up the server with you, explaining each command before it runs.

**8. Backups and alerts (30 minutes)**

Done on 27–28 October. The steps below are the ones actually used, kept for a rebuild. Every key, ping URL and backup code went into the password manager, never into a chat or a screenshot.

Cloudflare R2, where the nightly backups go:

1. Create a free account at [dash.cloudflare.com](https://dash.cloudflare.com/sign-up) with email and password, and verify the email. Turn on two-step login (person icon, My Profile, Authentication, Two-Factor Authentication) and save the backup codes in the password manager.
2. Left menu Storage & databases, R2 Object Storage. Add a card: Cloudflare needs one even on the free tier. On the Overview page, Add Budget Alert: $5. It emails a warning but doesn't stop charges.
3. Create bucket: name `btc-options-backup`, location Automatic, default storage class Standard (only Standard counts towards the free 10 GB). Public access stays disabled.
4. R2 Overview, API Tokens, Manage, Create Account API token: name `recorder-1-backup`, permission Object Read & Write, applied to `btc-options-backup` only, TTL forever, no IP filter (the laptop also pulls backups).
5. From the one-time result page, copy the Access Key ID, the Secret Access Key and the Default S3 endpoint into a password-manager note in `.env` format: `R2_ACCESS_KEY_ID=`, `R2_SECRET_ACCESS_KEY=`, `R2_ENDPOINT=`, `R2_BUCKET=btc-options-backup`. The Token value isn't needed.

You don't run anything yourself. Claude Code installs the backup tool (rclone) on the server, which uploads every night and test-restores once a month.

healthchecks.io, which emails you if something stops:

1. Sign up at [healthchecks.io](https://healthchecks.io/) with your email. There's no password at sign-up: it emails a sign-in link. Set a password afterwards in Account, Account Settings.
2. On the same page, turn on two-step login with the authenticator app. Healthchecks gives no backup codes, so also add a security key (Windows Hello) as a second factor.
3. Delete the default "My First Check", then add "recorder data flowing" (period 5 minutes, grace 5 minutes), "nightly backup" (1 day, grace 2 hours) and "disk space" (1 hour, grace 1 hour). Leave the slugs empty. Alerts go to your email.
4. Save each ping URL in the password manager in `.env` format: `HC_PING_URL_RECORDER=`, `HC_PING_URL_BACKUP=`, `HC_PING_URL_DISK=`. Ping URLs are secrets, and the checks list shows them in a column, so never screenshot it with that column showing.

**9. Domain and public dashboard (Phase 6)**

1. In Cloudflare: Domain Registration, Register Domains, then search for damiantrades.com (or damian00trades.com if it's taken). Cloudflare sells domains at cost; a .com was about $10.46 a year in 2026.
2. Zero Trust: pick the Free plan, which covers up to 50 users.
3. Networks, Tunnels, Create a tunnel, and save its token in the password manager. The server's firewall stays SSH-only, because the tunnel connects outwards.
4. Nothing on the public site can change the system; admin actions run from your laptop.

We'll do the exact clicks together in Phase 6.

**10. Deribit testnet key (Phase 8)**

1. Register at [test.deribit.com](https://test.deribit.com/). It's a separate site and account from real Deribit, with play money.
2. Account (person icon), API, Add new key. Name it `paper-bot`; give it account read and trade read/write, and no wallet or withdrawal rights.
3. Save the Client ID and Client Secret in the password manager. The secret is shown only once.
4. Add test funds from the testnet deposit page: BTC, and USDC if it's offered.

**11. Files to share (week 1)**

- We start from scratch, so no old code, handover or spec is carried over.
- Make sure btc-vol-models and BTC-Options-Strategy- are in your GitHub account; Claude Code reads them from there once you're signed in.
- Send JEV's details: what it takes in, what it returns, how it was trained and where it runs.
- Old-site API keys aren't reused; any key this project needs is made fresh (decided 14 November).

## Details to fill in

The first table holds only non-secret details. The server's IP address, the R2 endpoint and the Cloudflare account ID aren't secret either, but they stay out of this doc and the repo, which goes public at release: you give the IP to Claude Code in chat, and the endpoint goes only into the server's `.env`.

| Detail | Value | From step |
| --- | --- | --- |
| GitHub username | damiandamian181061-bit (confirmed) | 5 |
| Repo name | btc-options-lab (created 27 October, private, empty) | 5 |
| Server provider and plan | DigitalOcean Basic Droplet `recorder-1`: 2 vCPU, 4 GB, 80 GB disk, $24 a month plus VAT (created 27 October) | 7 |
| Server location | London (LON1) | 7 |
| Server firewall | DigitalOcean cloud firewall `ssh-only`: inbound SSH (TCP 22) only, outbound open | 7 |
| Server IPv4 address | Kept out of this doc and the repo; given to Claude Code in chat | 7 |
| Ubuntu version (laptop and server) | 26.04 LTS on both | 2, 7 |
| R2 bucket name | btc-options-backup (created 28 October; Standard storage class; public access disabled) | 8 |
| R2 S3 endpoint URL | Kept out of this doc and the repo; it goes only in the server's `.env` as `R2_ENDPOINT` | 8 |
| R2 access key | Account API token `recorder-1-backup`: Object Read & Write on btc-options-backup only; keys in the password manager | 8 |
| R2 budget alert | $5 a month (emails a warning; doesn't stop charges) | 8 |
| healthchecks check names | recorder data flowing (5 min, grace 5 min), nightly backup (1 day, grace 2 h), disk space (1 h, grace 1 h); created 27 October | 8 |
| Claude plan | Pro for now (decided 9 November); upgrade to Max if usage limits slow the build | 4 |
| Claude Code settings | Opus 5.5 at xhigh effort; starts in Manual mode (asks before commands and edits); SSH key loaded with keychain (all 9 November) | 4, 6 |
| Laptop memory | 16 GB, fixed (Core Ultra 5 226V); WSL capped at 8 GB | 2 |
| Domain | damiantrades.com, or damian00trades.com if taken | 9 |
| Selector | Fixed rules, with JEV as challenger (details to send) | 11 |

The second table lists every secret and where it lives. None of them ever goes in this doc, a chat, or the repo.

| Secret | Kept in | Used from | When |
| --- | --- | --- | --- |
| Account passwords and two-step codes | Password manager | Nowhere else | Now |
| SSH private key | Your laptop only (passphrase in the password manager) | Never copied anywhere | Now |
| R2 Access Key ID and Secret Access Key | Password manager | Server `.env`, typed in by you | Now |
| healthchecks ping URLs | Password manager | Server `.env` | Now |
| Cloudflare Tunnel token | Password manager | Server `.env` | Phase 6 |
| Deribit testnet Client ID and Client Secret | Password manager | Server `.env` | Phase 8 |
| Tardis API key, only if you buy history later | Password manager | Laptop or server `.env` | Not needed now |
| JEV API key, if it has one | Password manager | Server `.env` | Phase 7 |

Claude Code creates a `.env.example` listing every variable name with blank values. You copy it to `.env` and paste the values in yourself. Claude Code's settings will block it from reading `.env` at all, using its [permission deny rules](https://docs.claude.com/en/docs/claude-code/settings).

If a secret is ever pasted somewhere by mistake, revoke it and make a new one; deleting the message is not enough.

## Costs

Running costs are about $24 a month plus VAT for the server, plus your Claude plan. Almost everything else is free, and no historical data is being bought. Prices as of October 2026, in each seller's own currency.

| Item | What it's for | Price | When |
| --- | --- | --- | --- |
| DigitalOcean 4 GB Droplet | Recorder, paper trading, dashboard | $24 a month, plus VAT | From now, for as long as you want recording and the demo |
| Cloudflare R2 | Nightly backups | No monthly fee. Free each month: 10 GB of Standard storage, 1 million writes and 10 million reads; then $0.015 per GB a month (100 GB is about $1.35). $5 budget alert set | From Phase 2; likely $0 at first, then grows slowly |
| healthchecks.io | Email alerts | Free, up to 20 checks | Set up 27 October; used from Phase 1 |
| GitHub, Docker Desktop, WSL, Bitwarden | Code, containers, passwords | Free | From now |
| Domain | Public demo address | About $10.46 a year for a .com at Cloudflare | Phase 6 |
| Cloudflare Tunnel | Puts the dashboard online without opening the server | Free | Phase 6 |
| Deribit testnet | Paper trading | Free | Phase 8 |
| Claude plan | Claude Code | Pro at $20 a month, staying on it for now (decided 9 November). Max 20x is $200 a month (fastest for a one-month build); Max 5x is $100 | The build month |
| Tardis.dev | Historical data | Not buying | Decided 25 October |

**Which Claude plan.** Pro and Max both include Claude Code, and their limits are shared between Claude and Claude Code. Each plan has a five-hour limit and a weekly limit; when you hit one, you either wait for the reset or pay for extra usage at API rates, which you can decline.

**If you stay on Pro.** Nothing breaks and no work is lost; it just goes slower. Full days of Claude Code will often hit the five-hour limit partway through each window, and the weekly limit before the week ends, so the one-month plan would likely stretch to two or three months.

Paying for extra usage at API rates fills the gaps, but for heavy use it usually costs more than Max. Max 5x ($100) sits in between. Max 20x ($200) is the one that fits full days for a month, and you can drop back to Pro afterwards.

**Tokens.** On a plan you don't pay per token: you pay the monthly price and work within the limits. I can't give an honest token total for the whole build, because it depends on how much the live data throws up. Small, well-defined tasks per session keep usage predictable.

**What the build month costs.** On Pro, which you're staying on for now: $20 for Claude, $24 plus VAT for the server, about $10 for the domain and $0–2 of R2, roughly $55 plus VAT. On Max 5x it's roughly $135 plus VAT, and on Max 20x roughly $235. After the build, keeping the recorder, paper trading and dashboard running costs about $24 a month plus VAT, plus a dollar or two of R2.

Sources: [Hetzner availability notice](https://pulsetic.com/status/hetzner/incidents/3687/) · [DigitalOcean 2026 prices](https://valebyte.com/en/blog/digitalocean-pricing-overview-and-5-cheaper-alternatives-in-2026/) · [Cloudflare R2 pricing](https://developers.cloudflare.com/r2/pricing/) · [healthchecks.io plans](https://www.capterra.com/p/249957/Healthchecksio/pricing/) · [Cloudflare Zero Trust free plan](https://tools.cloudtweaks.com/company/cloudflare-access) · [Cloudflare .com price example](https://wellstsai.com/en/post/transfer-domain-to-cloudflare/) · [Claude Code with Pro or Max](https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan) · [Claude plan prices](https://dupple.com/learn/claude-pro-vs-max) · [Tardis.dev pricing](https://tardis.dev/)

## Tardis.dev: what it is and whether to buy

Decided on 25 October: no purchase. Backtests run on our own recording, which grows every day, and the results state exactly which period they cover. This section stays as background in case you change your mind later.

**What it is.** Tardis.dev records exchanges' live data feeds and sells the history. For Deribit it covers every instrument, including all options, since 30 March 2019.

**Why VS Code can't replace it.** VS Code is the editor where the code lives; it can't produce past market data. Deribit's API serves live quotes but not past order books, so the only sources are our own recorder, from today, or a vendor that recorded the past, like Tardis.

**How it works.** You subscribe to a data plan (the Options plan covers Deribit options) and download daily CSV files.

- The file that matters most for us is `options_chain`: every option's bid, ask, sizes, implied vols, mark, Greeks and underlying price, at each update.
- Other files cover quotes, trades, order book snapshots, and perp and futures tickers.
- We'd convert these into the same one-minute Parquet layout our recorder writes, so the backtester can't tell the two sources apart.

**What's free.** The first day of every month, for every exchange and data type, with no account. There's also a one-month free trial with no card: it covers days 1 to 10 of the previous month.

**Prices and terms for the Options plan**

| Type | Price | Includes | History you get |
| --- | --- | --- | --- |
| Academic | $350 a month, quarterly or yearly billing only | CSV files only. Needs a university email, such as one ending in .ac.uk | 12 months on quarterly billing (15 if paid by bank transfer), 4 years on yearly |
| Solo | $700 a month | CSV files | 4 months on monthly billing, 12 on quarterly, 4 years on yearly |

There are no refunds and no one-off purchases. Monthly and quarterly subscriptions renew until you cancel.

**Why it could be worth it.** Our own recording only starts now. By week 3 it will hold only two to three weeks of data, probably a single market regime. One quarter of Academic adds a year of quotes across several regimes, which makes the results far more convincing.

**How we handle it now**

1. The recorder runs from Phase 1, and every day it runs adds to the backtest data.
2. If time allows in Phase 2, an importer reads Tardis's free first-of-month files. These single-day snapshots from past years help check the pricing and surface code in other market conditions.
3. Buying history later stays possible, and the importer would already handle its format.

**Caveats**

- The data comes under Tardis's licence terms, so raw files stay private. Only our results and charts get published.
- The files are large, so we filter to BTC options and downsample to one-minute snapshots as they download.
- Before paying, we check that the free sample includes BTC\_USDC options with bid and ask sizes.

Sources: [Tardis.dev pricing](https://tardis.dev/) · [Billing and subscriptions FAQ](https://docs.tardis.dev/faq/billing-and-subscriptions) · [Deribit data details](https://docs.tardis.dev/historical-data-details/deribit)

## Trading plan v1: the theory

The plan sells BTC credit spreads only when options look expensive compared with the volatility that actually follows, with a capped loss on every trade. The backtest's job is to show whether that edge survives real costs. It is a research hypothesis for paper trading, not investment advice.

**Why it could work**

- Variance risk premium: option buyers pay for protection, so implied volatility has tended to sit above the volatility that follows. Selling options collects that gap.
- Time decay: an out-of-the-money option loses value each day the price stays away from its strike.
- Capped risk: buying the further-out leg limits the loss. That matters for BTC, which gaps more often than a normal distribution predicts.
- Trend filter: selling puts in an uptrend and calls in a downtrend keeps the short strike on the side the market is moving away from.

**What has to be true**

| # | Hypothesis | How the backtest tests it |
| --- | --- | --- |
| H1 | 30-day implied vol is above the realised vol that follows, on average | Implied vol minus the next 30 days' realised vol, across all the data |
| H2 | v1 spreads earn positive P&L after fees and bid-ask | v1 rules against no-trade and hold-to-expiry on the locked test |
| H3 | The volatility filter adds value | Rules with and without the implied-minus-realised filter |
| H4 | The trend filter cuts losses | Trend-chosen side against selling both sides every time |
| H5 | The 50% take-profit and 2× stop beat holding to expiry, risk-adjusted | Exit rules against hold-to-expiry, compared on Sortino and Expected Shortfall |
| H6 | Tail losses stay inside the risk budget under stress | Stress tests: BTC ±20%, implied vol +30 points |

**Playbook by market condition**

| Condition | What the plan does |
| --- | --- |
| Implied vol well above realised vol | Trade normally |
| Implied vol near or below realised vol | Stand aside: no trade is a valid decision |
| Uptrend, BTC above its 20-day average | Bull put spreads only |
| Downtrend, BTC below its 20-day average | Bear call spreads only |
| Shock: BTC moves 8% in a day, or implied vol jumps 10 points | No new entries for 24 hours; open spreads run to their exits |
| Thin or stale quotes | No entries |
| Big scheduled events, such as US rate decisions | Tested as a variant: no entries in the 24 hours before |

**Trade lifecycle**

1. Every minute, rebuild the surface and features, then check the filters.
2. Generate candidate spreads and drop any that fail the liquidity or credit rules.
3. Rank the survivors by the objective, or choose no trade.
4. Size to the 2% maximum-loss rule; the risk gateway checks every limit.
5. Record the thesis, for example "BTC stays above the short strike until expiry; buy back at half the credit".
6. Manage by rule only: take profit, stop or time exit, with no adjustments or rolls in v1.
7. Review weekly: thesis hit rate, net P&L, Expected Shortfall and drawdown against the benchmarks.

**When we'd drop it.** If v1 doesn't beat no-trade on the locked test after costs, or its Expected Shortfall breaks the budget under stress, the write-up says so. An honest negative result still shows that the platform works and that you can judge a strategy properly.

## Trading rules v1 (confirmed 25 October)

These are the starting values for the fixed-rule credit-spread strategy, confirmed on 25 October with paper capital raised to 100,000 USDC. Each one becomes a line in the strategy and risk config files.

**Universe and entry**

| Rule | Proposed value | Why |
| --- | --- | --- |
| Strategies | Bull put and bear call credit spreads | The blueprint's V1 scope |
| Book | BTC\_USDC linear options; inverse BTC options if the Phase 2 liquidity check finds the USDC book too thin | Fills must be realistic |
| Days to expiry at entry | 7 to 30 | Enough premium without the sharpest near-expiry gamma |
| No new entries | Within 48 hours of expiry | Avoids expiry-day gamma and settlement noise |
| Short-leg delta | 0.15 to 0.30, aiming for 0.20 | Balances credit received against the chance of being breached |
| Width | Nearest listed strikes giving 2% to 6% of the forward | Caps the loss per spread |
| Minimum credit | Net credit after fees, at executable bid/ask, of at least 20% of the width | The premium has to pay for the risk |
| Volatility filter | 30-day ATM implied vol at least 3 vol points above 30-day realised vol | Sell only when the variance risk premium is positive |
| Direction | Bull put when BTC is above its 20-day average, bear call when below | Simple trend filter; a both-sides version runs as a variant |
| Liquidity | Both legs two-sided; bid-ask spread at most 10% of mid; top-of-book size at least our order size; quotes under 2 minutes old | No fills that wouldn't really happen |
| Data quality | No entry while any data-quality flag is active | A data gap or crossed book must never trigger a trade |
| Entry pace | At most 2 new spreads per day | Spreads entries across time |
| Shock filter | No new entries for 24 hours after BTC moves more than 8% in a day, or 30-day ATM implied vol rises more than 10 vol points in a day | Lets the market settle after a jump |

**Exits**

| Rule | Proposed value | Why |
| --- | --- | --- |
| Take profit | Close when the spread costs 50% or less of the credit received to buy back | Banks most of the premium early |
| Stop loss | Close when buying back costs 2 times the credit or more, a loss of about one credit | Stops one bad trade erasing many good ones |
| Time exit | Close 24 hours before expiry | Hold-to-expiry still runs as a benchmark |
| Cooldown | No new spread on the same side for 24 hours after a stop loss | Avoids re-entering straight into a trend |

**Sizing and limits**

| Rule | Proposed value |
| --- | --- |
| Paper capital | 100,000 USDC |
| Risk per spread | Size so the maximum loss (width minus credit) is at most 2% of equity |
| Open spreads | At most 5, and at most 2 per expiry |
| Total risk | Sum of maximum losses at most 10% of equity |
| Portfolio delta | Net delta times BTC price at most 20% of equity, either direction |
| Portfolio vega | Vega per 1 vol point at most 0.5% of equity, either direction |
| Daily loss limit | No new trades after a 3% loss within one UTC day |
| Kill switch | No new trades after a 10% drawdown from peak equity, until you review |

**How success is judged**

- Per trade, your definition: at entry each trade records its thesis, for example "BTC stays above the short strike until expiry". At exit it records whether the thesis was met, and every run reports the thesis hit rate.
- Strategy level: net P&L after fees and slippage beats the benchmarks on the locked test period, with Expected Shortfall and maximum drawdown inside the risk budget.
- Why both: short-premium strategies can meet their thesis on most trades and still lose money overall. A few large losses can outweigh many small wins, and hit rate alone would hide that.

**Ranking weights.** The selector and the candidate ranking use the blueprint's objective:

```latex
\max_{c \in \mathcal{F}} \; \mathbb{E}^{\mathbb{P}}\left[\mathrm{PnL}_{\text{net}}(c)\right] - \lambda\,\mathrm{ES}_{\alpha}(c) - \kappa\,\mathrm{Turnover}(c) - \mu\,U(c)
```

Starting values, recommended and confirmed: α = 97.5%, the Expected Shortfall level in Basel market-risk rules; λ = 1; κ = 0, because fees and slippage are already in net P\&L; μ = 0 until a model-uncertainty measure exists. All of them are tuned on validation data only, never on the locked test.

## Build plan

Eleven phases, each closed by a gate. Working full days, Phases 0 to 9 fit into four weeks, with release v1.0 at the end of week 4 and the challenger models in week 5. A rough end-to-end version, from recorder to paper trades to a Streamlit page, runs by the end of week 1; each later phase replaces a rough part with a tested one.

**Roadmap at a glance** (text version of the diagram in the online doc; the recorder runs 24/7 from Phase 1 onwards)

| Phase | When | Gate before the next phase |
| --- | --- | --- |
| 0 · Setup | Week 1 · days 1–2 | Laptop, server, repo, backups and alerts all working |
| 1 · Recorder live | Week 1 · days 2–4 | 24 h with no gap over 5 min; day-one volume measured |
| 2 · Data and checks | Week 1 · days 4–7 | Restore from R2 works; every day checked; book chosen |
| 2B · Rough end-to-end version | End of week 1 | Paper service runs 24 h and shows its trades (results don't count yet) |
| 3 · Pricing and surface | Week 2 | IVs and Greeks match Deribit and the golden tests |
| 4 · Backtest engine | Weeks 2–3 | Determinism, leakage and reconciliation tests pass |
| 5 · Strategies and results | Week 3 | v1 rules vs every benchmark on the locked test |
| 6 · Streamlit dashboard | Weeks 3–4, in parallel | Public and read-only; nothing on it changes the system |
| 7 · JEV challenger | Week 4 | JEV vs rules on the same frozen folds |
| 8 · Testnet paper trading | Weeks 3–4, runs a week | A week of paper decisions matches its replay |
| 9 · Release v1.0 | End of week 4 | A fresh clone runs end to end with one command |
| 10 · Challenger models | Week 5, after v1.0 | Each challenger promoted or rejected on evidence |

The dashboard and paper trading overlap the phases above them, and a rough end-to-end version runs by the end of week 1.

Phase 2B status: deployed on 15 November 2026 at 15:30 UTC. Its gate was checked on 17 November: the paper service ran over 24 hours and decides daily, but no rough trade yet (implied vol below realised vol each day). The gate stays open until the first rough trade shows, and Phase 3 starts meanwhile; the rules are not loosened.

### Phase 0: Setup (week 1, days 1–2)

- Builds: your setup steps; repo skeleton (Python 3.12, uv, ruff, mypy, pytest, pre-commit with secret scanning, GitHub Actions CI); `CLAUDE.md` and `docs/progress.md`; server hardening (non-root user, SSH keys only, SSH-only firewall, automatic security updates, time sync, Docker).
- Gate: Docker and Claude Code work on the laptop, the server answers over SSH, the repo exists, and backups and alerts are configured. Passed on 10 November.

### Phase 1: Recorder live (week 1, days 2–4)

- Builds: settings that refuse to start if `LIVE_TRADING=true`; Deribit WebSocket client with heartbeats, reconnect with backoff, resubscribe, rate limits and several connections; instrument discovery for BTC\_USDC and inverse BTC options, perps and futures, every 10 minutes and after each 08:00 UTC expiry; minute snapshots into TimescaleDB; raw top-of-book and trade stream into hourly compressed Parquet; health pings sent only when fresh rows land; disk alert; Docker Compose deploy with automatic restarts.
- Gate: 24 hours with no gap over 5 minutes, and day-one data volume measured, with the raw-stream budget set from it. The recorder went live on 13 November; its first 24 hours end at about 16:35 UTC on 14 November.

### Phase 2: Data platform and checks (week 1, days 4–7)

- Builds: nightly Parquet export with manifest and checksums, uploaded to R2, and a tested restore; checks for gaps, crossed books, stale quotes and IV outliers, written to `data_quality`; liquidity report comparing the linear and inverse books; point-in-time `MarketDataView` with leakage tests; Tardis importer for the free first-of-month files, if time allows; a first Streamlit status page.
- Gate: a restore from R2 works, the gap report covers every day, and the liquidity report settles which book the strategies use.

### Phase 3: Pricing and volatility surface (week 2)

- Builds: conventions doc; Black-76 prices and Greeks for linear options, with BTC-denominated pricing for inverse options; IV solver (Newton with a Brent fallback); forward per expiry; spline smile fit and surface buckets (1 to 90 days; 10-delta, 25-delta, ATM) interpolated in total variance, with no-arbitrage checks; surface and underlying features; formula sheet and accounting rules for your sign-off.
- Gate: IVs and Greeks match Deribit's and finite differences within agreed tolerances, golden tests match your btc-vol-models numbers, and the live surface shows on the status page.

### Phase 4: Backtest engine (weeks 2–3)

- Builds: event loop over point-in-time snapshots; fills that cross the spread at top of book, with partial fills beyond it; fee schedule with the premium cap; perp trading and funding for the hedged benchmarks; risk gateway with logged rejections; double-entry ledger; settlement at Deribit's delivery price, including the two-step settlement through the expiry's future; run records (config, git commit, dataset hash, seed); results tables and MLflow, with backtests run from your laptop.
- Gate: determinism, leakage and reconciliation tests pass on a real week, and a hand-checked smoke trade matches.

### Phase 5: Strategies and evaluation (week 3)

- Builds: credit-spread candidate generator and the v1 fixed-rule selector; thesis logging; both benchmark sets; metrics (Sharpe, Sortino, Calmar, drawdown, VaR and Expected Shortfall at 95% and 99%, worst day, thesis hit rate); Greek and strategy P&L attribution; stress tests; cost sensitivity from 0.5 to 3 times; walk-forward and locked-test runs; results report.
- Gate: a full report of the v1 rules against every benchmark on the locked test period, after costs.

### Phase 6: Streamlit dashboard (weeks 3–4, alongside Phases 4 and 5)

- Builds: Streamlit dashboard reading the database and results directly, with pages for the market, the volatility surface, the backtest list and each run (equity, drawdown, metrics, attribution, Greeks, trade inspector), plus health; put online through Cloudflare Tunnel at your domain.
- Gate: the public site works, and nothing on it can change the system.

### Phase 7: JEV as challenger (week 4)

- Builds: a selector interface; the JEV adapter once you send its details; GEV/EVT tail model feeding Expected Shortfall into candidate features; JEV runs against the fixed rules on the same frozen folds, with a random-selection floor and instant rollback.
- Gate: a JEV-versus-rules report on the same folds, seeds and cost model.

### Phase 8: Paper trading on testnet (starts end of week 3, runs a week)

- Builds: paper service running the same strategy code on live data; testnet orders, using combo orders where available; reconciliation; kill switch.
- Gate: a week of paper decisions matches a replay of the same days.

### Phase 9: Release v1.0 (end of week 4)

- Builds: README with architecture diagram, setup, results and credits; MIT licence; repo made public; screenshots.
- Gate: a fresh clone runs end to end with one command.

### Phase 10: Challenger models (week 5, after v1.0)

- Builds: Heston and Bates calibration ported from btc-vol-models; EWMA and GARCH volatility forecasts; promotion only on out-of-sample wins.
- Gate: each challenger is promoted or rejected by the blueprint's rules, with the evidence recorded.

After v1.0, the recorder and paper trading keep running for as long as you like, for weeks or months. The backtests re-run each month on the growing data, and the results section is updated each time. The RL exit policy and an LLM research assistant come later, as in the blueprint's later phases.

## What could go wrong

The failures that matter most are the recorder silently stopping, a backtest that looks good for the wrong reasons, and a leaked key. Each risk below has a named defence that gets built and tested.

| Area | What could go wrong | How it's handled |
| --- | --- | --- |
| Recording | The recorder runs but stops writing | Health pings are sent only when fresh rows land, so you get an email within 10 minutes. Automatic restart. Gaps are labelled and skipped by backtests. |
| Recording | Server outage, or the provider refuses new servers | Docker restarts everything on reboot. The server can be rebuilt from the repo's setup script and R2 backups, on DigitalOcean or another provider. Gaps are recorded, never filled with guesses. |
| Recording | Raw data fills the disk | Day-one measurement sets the raw budget. Local files move to R2 after 7 days. Disk alerts at 70% and 85%. |
| Recording | Deribit changes its API, renames instruments or disconnects | Fields are read from instrument metadata, not hard-coded. Unknown messages raise an alert. Reconnect with backoff and resubscribe. |
| Recording | The USDC book is too thin to trade | The inverse book is recorded from day one, and the Phase 2 liquidity report picks the book before strategies are built. |
| Backtest | Look-ahead: decisions use data from the future | Every read goes through the point-in-time view. A leakage test changes all data after time t and checks decisions at t don't move. |
| Backtest | Fills at mid or mark make results look better than reality | Fills only at executable bid/ask, within quoted size. Mark and mid appear only in labelled comparison runs. Costs are stress-tested up to 3 times. |
| Backtest | Too little history to trust the result | The report states which market regimes it covers. Our recording grows every day. No claims beyond the data. |
| Backtest | Overfitting by trying many rule variants | The locked test period stays untouched until the end, every run is logged, and success metrics are fixed in advance. |
| Backtest | High hit rate hides rare large losses | Expected Shortfall, stress tests and drawdown are judged alongside the thesis hit rate. |
| Finance | Unit mix-ups: USDC vs BTC premiums, vega per 1% vs per 1.0, days vs years | One conventions doc. Money values carry their currency. Tests compare our Greeks with Deribit's. |
| Finance | Accounting errors | Double-entry ledger. A reconciliation test checks that option, hedge, fee and funding P&L sum to the change in equity at every step. |
| Finance | Settlement handled wrongly, including the two-step settlement through the expiry's future (linear options since April 2026, inverse since 1 August 2026) | Settlement lives in one module and is reconciled against Deribit's delivery prices. |
| Finance | Maths that looks right but isn't | Golden tests against your btc-vol-models numbers and finite differences. You review every maths change. |
| Claude Code | Context lost between sessions | `CLAUDE.md`, this brief and the progress log are read at every start. One task at a time, committed once its tests pass. |
| Claude Code | Usage limit hit mid-task | Tasks are sized to finish within a session, and the progress log means nothing is lost when a limit hits. |
| Claude Code | You approve a command you don't understand | Every server command is explained in plain words first. Anything destructive needs your explicit yes and a verified backup. |
| Laptop | Windows quirks: paths, line endings, slow file access | Everything runs inside WSL, with the repo in the Linux home folder rather than on the C: drive. |
| Laptop | Disk (256 GB) or memory (16 GB) runs out | Full history stays in R2; the laptop pulls only what a backtest needs. WSL is capped at about 8 GB. |
| Laptop | Laptop lost or broken | Code is pushed to GitHub after every task; data lives on the server and R2. |
| Security | A secret leaks into the doc, a chat or the repo | Secrets live only in `.env`, which Claude Code is blocked from reading. Secret scanning runs before every commit and in CI. A leaked key is revoked and replaced. |
| Security | Someone breaks into the server | SSH keys only, no password or root login, an SSH-only firewall and automatic security updates. The website reaches the server through Cloudflare Tunnel. |
| Security | The public demo gets abused | The site is read-only with no admin controls, and Cloudflare sits in front of it. |
| Security | Live trading switched on by accident | The program refuses to start with `LIVE_TRADING=true`, and no live Deribit keys exist anywhere. |
| Cost | Costs creep up | Monthly cost check, an R2 storage budget, and any Tardis subscription cancelled straight after the download. |
| Legal | Vendor data published by mistake | Raw Tardis files are never committed or published, only results. |
| People | JEV turns out hard to integrate | Fixed rules ship regardless. Any selector sits behind the same interface, as a challenger. |
| People | Keys from the old site still active | No old-site key is reused; any key this project needs is made fresh (decided 14 November). |
| Schedule | A four-week build squeezes testing | Gates never get skipped to save time. If a gate fails, the schedule moves, not the standard. |
| Expectations | The rough week-1 version shows profits that come from a bug, not an edge | The week-1 version is labelled rough. No result counts until the Phase 4 fill, fee and accounting tests pass. |

## Rules for Claude Code

This section becomes `CLAUDE.md`, the file Claude Code [loads at the start of every session](https://docs.claude.com/en/docs/claude-code/settings). Each rule holds unless Damian changes it.

**Every session**

- Start by reading `CLAUDE.md`, `docs/brief.md` and `docs/progress.md`, then state the task being taken and its gate.
- Work on one task from `docs/progress.md` at a time. Write the tests first for maths, accounting and settlement.
- Finish with every test passing, a clear commit, a push to GitHub, and an updated progress log: done, next, open questions.
- When the finance intent is unclear, ask Damian. Never guess a trading or accounting rule.

**Conventions**

- Timestamps in UTC milliseconds; τ in years (ACT/365) to the 08:00 UTC expiry; IV as a decimal; log-moneyness k = ln(K/F).
- Every money value carries its currency: USDC for linear options, BTC for inverse option premiums. Size is in BTC contracts.
- Vega and theta use Deribit's units, checked against Deribit's own Greeks in Phase 3, so the two compare directly.
- The cash ledger uses exact decimals; pricing maths uses floating point.
- All market data is read through `MarketDataView(as_of=t)`, and nothing reads data after t.
- Fills happen at executable bid/ask within quoted size. Mark and mid appear only in runs labelled as comparisons.

**Safety**

- `LIVE_TRADING` must be false. The program refuses to start otherwise, and no live keys ever exist.
- Never read, print or commit `.env` or any secret. Settings deny reading `.env`, and secret scanning runs before every commit and in CI.
- Explain every server command in plain words before running it. Never delete data, drop tables or wipe disks on the server without Damian's explicit yes and a verified backup.
- Back up before every database migration on the server.
- Never commit raw vendor data, and never put the server's address in the repo.

**Quality**

- Every maths function has golden tests: btc-vol-models numbers, Deribit's values and finite differences.
- Ruff, mypy and pytest pass before every commit, and CI is green before merging.
- A phase ends only when its gate passes, with the evidence recorded in `docs/progress.md`.

**Scope**

- New ideas go in `docs/later.md`, not into the current phase.
- Keep the module boundaries from the repo layout. Strategies, JEV and any AI never touch execution; only the risk gateway passes orders on.

## README credit

This is the credit text for the README. It is plain about who did what, which is also the strongest position in an interview: you specified the finance, set the risk rules and judged the results.

```markdown
## Credits

Built by Damian Craddock with Claude Code, Anthropic's coding agent.

Damian designed the trading approach and the maths and finance
specification, made every design decision and reviewed each change.
Claude Code wrote the code and tests to that specification.
Omer and Reggie contributed to the early planning.
```

## Questions still open for you

Answer these in the doc or in chat. Once they're settled and setup is done, we export this brief into the repo and start Phase 0.

- [x] Claude plan: decided 9 November. Staying on Pro for now, upgrading if usage limits slow the build.
- [ ] JEV: you research it first. Prompt 7 needs its inputs and outputs, whether the same inputs always give the same answer, speed and cost per call, how and on what dates it was trained, and whether you can run it yourself. It's needed by week 4 and doesn't hold up the start.
- [x] Start: done. This brief went into the repo as `docs/brief.md` and Prompt 0 was pasted on 9 November. Phase 0 passed on 10 November, and the recorder went live on 13 November.

---

# Part 2: Claude Code prompts

Paste one prompt at a time into Claude Code from the project folder, in VS Code or the Ubuntu window. Wait for its report and check its gate before pasting the next one.

**Before Prompt 0.** Finish setup steps 1 to 8, then save the Build Brief as Markdown in the project folder as `docs/brief.md`. I'll export it for you when we start.

**No keys in prompts.** Each prompt names the variables it needs; you paste the values into `.env` yourself, and Claude Code is blocked from reading that file.

**Gates that need time.** Some gates need hours or days, such as 24 hours of recording or a week of paper trading. When a prompt stops to wait, come back later and paste: `Check the Phase N gate and report.`

**When something breaks.** Every prompt tells Claude Code to stop and explain rather than guess. Paste its explanation into our chat if you want a second opinion.

## Prompt 0: Setup (Phase 0)

Needs setup steps 1 to 8 done and the server's IP address to hand.

```text
Phase 0: Setup. Read docs/brief.md in full first, then tell me in two lines what you'll do and what the gate is.

1. Create CLAUDE.md from the brief's "Rules for Claude Code" section, plus a short project summary and the repo layout. Create docs/progress.md listing Phases 0 to 10 from the brief, all open.
2. Create the repo skeleton from the brief's repo layout: one Python package src/optlab (Python 3.12, managed with uv), apps/worker, apps/dashboard, infra/, configs/, tests/, research/, docs/. Add ruff, mypy (strict for src/), pytest, pre-commit with a secret scanner, and a GitHub Actions workflow that runs lint, type checks and tests on every push.
3. Add .gitignore (.env, data/, *.parquet, local files) and .gitattributes forcing LF line endings. Add .env.example with these variables and empty values: R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ENDPOINT, R2_BUCKET, HC_PING_URL_RECORDER, HC_PING_URL_BACKUP, HC_PING_URL_DISK, CLOUDFLARE_TUNNEL_TOKEN, DERIBIT_TESTNET_CLIENT_ID, DERIBIT_TESTNET_CLIENT_SECRET, JEV_API_KEY, TARDIS_API_KEY and DATABASE_URL; plus TRADING_MODE=paper, LIVE_TRADING=false and DERIBIT_ENV=public. Add .claude/settings.json that denies reading .env and .env.* files.
4. Add src/optlab/config.py: a pydantic-settings Settings class that refuses to start if LIVE_TRADING is true, with a test proving it.
5. Connect this folder to the private GitHub repo damiandamian181061-bit/btc-options-lab, helping me sign in if needed, and push.
6. Server: I'll give you the IP. Connect over SSH as root with my key, explaining each command before you run it. Create a user called deploy with my key, check that I can log in as deploy, then disable root and password logins. Turn on automatic security updates, UTC time with time sync, Docker with the compose plugin, 2 GB of swap and Docker log rotation. About this server: it's a DigitalOcean Droplet called recorder-1 (London, Ubuntu 26.04). Its firewall is a DigitalOcean cloud firewall called ssh-only, set in the control panel; if you also turn on ufw, allow SSH on port 22 before enabling it. It has updates waiting and says a restart is required: install them and restart it, after asking me. DigitalOcean's metrics agent is installed; leave it. If I'm ever locked out, the Web Console on the Droplet's page is the way back in.
7. On the server, clone the repo with a read-only deploy key into /srv/btc-options-lab and create .env there from .env.example. Stop at that point: I'll paste the secret values myself.
8. Write infra/server-setup.md documenting every step, so the server can be rebuilt from scratch, including how to create the Droplet: DigitalOcean, London (LON1), Ubuntu 26.04 LTS x64, Basic plan with Regular CPU (2 vCPU, 4 GB, 80 GB SSD), my SSH key, public IPv4 on, IPv6 off, monitoring on, backups off, cloud firewall ssh-only. Never write the server's IP address, the R2 endpoint or the Cloudflare account ID into any file: use placeholders such as SERVER_IP.

Gate: CI is green, I can SSH in as deploy, root login is refused, and docker run hello-world works on the server.

Ask me before anything that deletes files, changes the firewall, or needs a password or key.

When you're done: run every test, commit and push, and update docs/progress.md with the gate result and its evidence. Then report to me in plain words, including anything I must do by hand. If the gate fails, stop, explain why and propose a fix; don't start the next phase.
```

## Prompt 1: Recorder live (Phase 1)

Needs the healthchecks ping URLs pasted into the server's `.env`. The gate needs 24 hours of recording.

```text
Phase 1: Recorder. Read CLAUDE.md, docs/brief.md and docs/progress.md first, then tell me in two lines what you'll build and what the gate is. Follow every rule in CLAUDE.md. Check docs.deribit.com whenever you're unsure how the API behaves.

Build each part with tests:
1. Database: Docker Compose services for db (PostgreSQL with TimescaleDB) and recorder. First Alembic migration with tables instruments; option_quotes (one row per option per minute: bid, ask, sizes, mark, mark IV, bid and ask IV, Greeks, open interest, underlying price, exchange and receive timestamps); underlying_ticks (index, perps with funding, dated futures); option_trades (deduplicated on trade_id); delivery_prices; connection_events. Make option_quotes and underlying_ticks hypertables, compressed after 7 days.
2. Deribit WebSocket client, public channels only, no key: JSON-RPC 2.0 with request ids mapped to futures; heartbeat via public/set_heartbeat, answering test_request with public/test; reconnect with exponential backoff and resubscribe; a token-bucket rate limiter; several connections if the number of subscriptions needs it. Raw Deribit JSON never leaves the exchange adapter; the rest of the code uses typed models. Test against a fake server replaying recorded messages, including disconnects.
3. Instrument discovery every 10 minutes and just after each 08:00 UTC expiry: BTC_USDC linear options, inverse BTC options, BTC perps and dated futures. Read strikes, expiries and contract sizes from the API, never by parsing instrument names.
4. Recorder: keep the latest ticker per instrument in memory and write one row per instrument every 60 seconds using COPY. Record trades and delivery prices, and every disconnect in connection_events. Also write a raw stream (best bid/ask changes and trades) to hourly zstd-compressed Parquet files under data/raw/.
5. Health: ping HC_PING_URL_RECORDER only when rows were written in the last 2 minutes. A disk check pings HC_PING_URL_DISK and warns at 70% and 85% full.
6. Deploy on the server with docker compose up -d and restart: unless-stopped, then stop and tell me it's running.

Gate (I'll come back after 24 hours and ask you to check it): no gap over 5 minutes in option_quotes for 24 hours, shown with the query and its result, plus a day-one volume report for the database and the raw files with a proposed raw-stream cap that keeps R2 under about $10 a month.

Stop and ask me if Deribit's API behaves differently from the brief, or if the server runs short of memory or disk.

When you're done: run every test, commit and push, and update docs/progress.md with the gate result and its evidence. Then report to me in plain words, including anything I must do by hand. If the gate fails, stop, explain why and propose a fix; don't start the next phase.
```

## Prompt 2: Data platform and checks (Phase 2)

Needs the R2 keys and the backup ping URL pasted into the server's `.env`.

```text
Phase 2: Data platform and checks. Read CLAUDE.md, docs/brief.md and docs/progress.md first, then tell me in two lines what you'll build and what the gate is. Follow every rule in CLAUDE.md.

1. Nightly job at 00:30 UTC: export the previous UTC day of option_quotes, underlying_ticks and option_trades (later also vol_surface and features) to zstd Parquet, partitioned by date, and by expiry for options. Write a manifest of row counts and checksums; its hash is the dataset version. Upload to R2 with rclone using R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ENDPOINT and R2_BUCKET. Keep 7 days of raw files on the server and older ones only in R2. Ping HC_PING_URL_BACKUP on success.
2. Restore test: a script that rebuilds a fresh database from R2 for a chosen day and compares row counts and checksums. Run it.
3. Hourly data checks: missing snapshots, crossed books, empty or stale quotes, IV outliers and a stale index, written to a data_quality table. Flag gap periods so backtests skip them.
4. Point-in-time MarketDataView(as_of=t) over Parquet days for backtests and over the database for live use, returning only rows with timestamps at or before t, plus an iterator over snapshot times. Leakage test: changing every row after t must not change anything returned at t.
5. Liquidity report in research/: compare the BTC_USDC and inverse BTC books by bid-ask spread, top-of-book size and trade count, for the strikes and expiries the v1 rules would trade, and recommend which book the strategies should use.
6. A first Streamlit status page in apps/dashboard: last tick per stream, recent gaps, data volume and recorder uptime.
7. A sync command on my laptop that pulls chosen days from R2 into data/ for local backtests.
8. Only if time allows: an importer for Tardis's free first-of-month options_chain files into the same Parquet layout.

Gate: the restore test passes, the gap report covers every recorded day, and the liquidity report names the book. Show the evidence.

When you're done: run every test, commit and push, and update docs/progress.md with the gate result and its evidence. Then report to me in plain words, including anything I must do by hand. If the gate fails, stop, explain why and propose a fix; don't start the next phase.
```

## Prompt 2B: Rough end-to-end version (end of week 1)

This gives you something running early. Its profit and loss is not a result; the later phases replace every shortcut. The gate needs 24 hours of running.

```text
Rough end-to-end version. Read CLAUDE.md, docs/brief.md and docs/progress.md first, then tell me in two lines what you'll build. Label everything from this step "rough" in the code and on screen.

1. A simple rule: once a day, if 30-day ATM implied vol (from Deribit's mark IV) is above 30-day realised vol, choose a bull put or bear call spread using the brief's trend rule, with the short leg near 0.20 delta and 7 to 30 days to expiry, using Deribit's own Greeks.
2. Paper fills at the current bid and ask. Take Deribit's option fees from its published fee page into configs/fees/deribit.yaml with an effective-from date, and ask me to confirm the numbers. Keep a simple ledger: take profit at 50% of the credit, stop at 2 times the credit, settle at the delivery price.
3. Run it as a paper service on the server, and show its positions, trades and equity on the Streamlit page.
4. Write docs/rough-version.md listing every shortcut taken, so later phases can replace each one.

Gate (I'll come back after 24 hours and ask you to check it): the paper service has run for 24 hours, the page shows its trades, and the shortcut list exists.

When you're done: run every test, commit and push, and update docs/progress.md. Then report to me in plain words, including anything I must do by hand.
```

## Prompt 3: Pricing and volatility surface (Phase 3)

You review the formula sheet and accounting rules this prompt produces before Phase 4 starts.

```text
Phase 3: Pricing and volatility surface. Read CLAUDE.md, docs/brief.md and docs/progress.md first, then tell me in two lines what you'll build and what the gate is. Follow every rule in CLAUDE.md. Write the tests before the code for every function in this phase.

1. docs/conventions.md: the units and conventions from CLAUDE.md, plus how inverse options (premium in BTC) convert to USDC.
2. Black-76 on the forward: price, delta, gamma, vega and theta for linear USDC options, and BTC-denominated pricing for inverse options. Vega and theta in Deribit's units. Test against finite differences, put-call parity, and reference values from my btc-vol-models repo (clone it from my GitHub).
3. Implied vol solver: Newton with a Brent fallback, no-arbitrage price bounds, and flags for unreliable quotes. Round-trip test: price to IV and back to price.
4. A forward per expiry: the matching dated future where one exists, otherwise put-call parity on near-ATM pairs.
5. Smile and surface: a smoothing spline in log-moneyness for each expiry, interpolated in total variance to 1, 3, 7, 14, 30, 60 and 90 days and to 10-delta, 25-delta and ATM, with calendar and butterfly no-arbitrage checks. A worker stores the surface every minute in vol_surface.
6. Features every minute, using only data at or before each timestamp: ATM IV, 25-delta risk reversal and butterfly, term-structure slope, close-to-close realised vol, IV minus realised vol, basis and funding. Store them in features.
7. docs/formulas.md (returns, realised vol, Sharpe, Sortino, Calmar, drawdown, VaR and Expected Shortfall, annualised over 365 days) and docs/accounting.md (how USDC cash, premiums, fees, settlement and funding are booked), each with a worked example. Ask me to review both.
8. Add the live surface and smiles to the Streamlit page.

Gate: on a recorded day, our IVs and Greeks match Deribit's within tolerances you propose and I agree (report the differences), the golden tests pass, and the surface shows on the page.

When you're done: run every test, commit and push, and update docs/progress.md with the gate result and its evidence. Then report to me in plain words, including anything I must do by hand. If the gate fails, stop, explain why and propose a fix; don't start the next phase.
```

## Prompt 4: Backtest engine (Phase 4)

This replaces the rough engine from Prompt 2B. You check the smoke trade's numbers by hand at the end.

```text
Phase 4: Backtest engine. Read CLAUDE.md, docs/brief.md, docs/progress.md, docs/accounting.md and docs/rough-version.md first, then tell me in two lines what you'll build and what the gate is. Follow every rule in CLAUDE.md. This phase replaces the rough engine.

1. Event loop over point-in-time snapshots: for each snapshot, build MarketState, call the strategy, send orders through the risk gateway, fill them, update positions and the ledger, and record equity.
2. Fills: cross the spread at top of book, with partial fills beyond the quoted size. Fees come from configs/fees, including the premium cap. Spreads fill leg by leg, each at its own bid or ask.
3. Risk gateway: every limit from configs/risk, matching the brief's sizing and limits table. Reject any order that breaches a limit and log each rejection in risk_events. Nothing reaches a fill without passing the gateway.
4. Double-entry ledger in exact decimals; settlement at Deribit's delivery price at 08:00 UTC, including the two-step settlement through the expiry's future (see Settlement in docs/brief.md: same payoff, one delivery fee per in-the-money leg); perp trading and funding for the hedged benchmarks.
5. Run records: config (YAML loaded into Pydantic), git commit, dataset hash and seed saved with every run; results tables backtest_runs, backtest_orders, backtest_fills, backtest_positions and backtest_equity; MLflow logging. Backtests run on my laptop from data synced from R2.
6. Tests: determinism (the same config twice gives identical result hashes); leakage (changing data after t leaves decisions at t unchanged); reconciliation (option, hedge, fee and funding P&L sum to the change in equity at every step); and a smoke trade, one ATM call held to expiry, that we check by hand together.

Gate: all four tests pass on a real recorded week. Show me the smoke trade's numbers step by step so I can check them.

When you're done: run every test, commit and push, and update docs/progress.md with the gate result and its evidence. Then report to me in plain words, including anything I must do by hand. If the gate fails, stop, explain why and propose a fix; don't start the next phase.
```

## Prompt 5: Strategies and evaluation (Phase 5)

This is where the trading plan meets the data. With only a few weeks recorded, the honest answer may be "not enough data yet"; the prompt asks for that rather than a stretched claim.

```text
Phase 5: Strategies and evaluation. Read CLAUDE.md, docs/progress.md and, in docs/brief.md, the sections "Trading plan v1: the theory" and "Trading rules v1" first. Then tell me in two lines what you'll build and what the gate is. Follow every rule in CLAUDE.md.

1. Candidate generator for bull put and bear call spreads using every v1 rule: expiry window, short-leg delta, width, minimum credit, volatility filter, trend filter, liquidity, data quality, shock filter and entry pace.
2. Fixed-rule selector with the exits, sizing and limits from the rules tables. Every trade records its thesis at entry, and whether the thesis was met at exit.
3. Benchmarks. Set A: no-trade, hold-to-expiry, take-profit/stop, volatility threshold, random valid selection. Set B: cash, BTC hold, long straddle, delta-hedged straddle, IV vs RV, calendar.
4. Metrics from docs/formulas.md plus thesis hit rate. Greek P&L attribution (delta, gamma, vega, theta, residual) and strategy attribution (strategy, hedge, fees, slippage, funding). Stress tests by full revaluation: BTC plus and minus 10% and 20%, IV plus 10 and 30 points and minus 20, spreads times 2 and 5. Cost sensitivity at 0.5 to 3 times.
5. Split the recorded data in time order into train, validation and a locked test period. Tune nothing on the locked test.
6. Test hypotheses H1 to H6 from the brief and write docs/results.md: what each test showed, the period and market conditions it covers, and what didn't work.

Gate: a full report of v1 against every benchmark on the locked test, after costs. If there isn't enough data yet for a meaningful locked test, say so plainly and tell me the date when there will be.

When you're done: run every test, commit and push, and update docs/progress.md with the gate result and its evidence. Then report to me in plain words, including anything I must do by hand. If the gate fails, stop, explain why and propose a fix; don't start the next phase.
```

## Prompt 6: Streamlit dashboard (Phase 6)

Needs the domain bought and the Cloudflare Tunnel token pasted into the server's `.env` (setup step 9).

```text
Phase 6: Streamlit dashboard. Read CLAUDE.md, docs/brief.md and docs/progress.md first, then tell me in two lines what you'll build and what the gate is. Follow every rule in CLAUDE.md.

1. Pages: market (option chain by expiry); volatility surface (a smile per expiry and an expiry by moneyness heatmap); backtests (a list with filters); run detail (config, equity and drawdown, metrics and tail risk, benchmark table, P&L attribution, Greeks over time, and a trade inspector showing each trade's thesis and outcome); paper trading (live positions and equity); and health.
2. Read the database and results directly, with cached queries. No page can change anything.
3. Publish through Cloudflare Tunnel at damiantrades.com, or damian00trades.com if that's what I bought: run cloudflared on the server with CLOUDFLARE_TUNNEL_TOKEN. The server firewall stays SSH-only.
4. A footer on every page: "Paper-trading research project, not investment advice."

Gate: the public site loads every page on a laptop and a phone, and nothing on it can change the system.

When you're done: run every test, commit and push, and update docs/progress.md with the gate result and its evidence. Then report to me in plain words, including anything I must do by hand. If the gate fails, stop, explain why and propose a fix; don't start the next phase.
```

## Prompt 7: JEV (Phase 7)

Written after our JEV walkthrough, once we know what JEV takes in and returns. It will cover the selector interface, the JEV adapter, the GEV/EVT tail model and the JEV-versus-rules report.

## Prompt 8: Paper trading on testnet (Phase 8)

Needs the Deribit testnet key pasted into the server's `.env` (setup step 10). The gate needs 7 days of paper trading.

```text
Phase 8: Paper trading on Deribit testnet. Read CLAUDE.md, docs/brief.md and docs/progress.md first, then tell me in two lines what you'll build and what the gate is. Follow every rule in CLAUDE.md, especially the safety rules.

1. A paper service on the server that runs the same strategy and risk code as the backtests, on live data, every minute.
2. Orders go to Deribit testnet only, using DERIBIT_TESTNET_CLIENT_ID and DERIBIT_TESTNET_CLIENT_SECRET with DERIBIT_ENV=test. Use combo orders where testnet offers them for these options; otherwise send legs one at a time, with a written rule for a half-filled spread.
3. Every hour, reconcile our ledger with testnet's positions and fills, and alert on any mismatch.
4. A kill switch: one command on the server that cancels every order and blocks new ones. Add the daily loss limit and the drawdown kill switch from the rules.
5. Any attempt to use a production URL or a production key must fail at startup, with a test proving it.
6. Start the service, then stop and tell me it's running.

Gate (I'll come back after 7 days and ask you to check it): replay the same 7 days through the backtester and compare decisions trade by trade. They must match, with every difference explained.

When you're done: run every test, commit and push, and update docs/progress.md with the gate result and its evidence. Then report to me in plain words, including anything I must do by hand. If the gate fails, stop, explain why and propose a fix; don't start the next phase.
```

## Prompt 9: Release v1.0 (Phase 9)

You confirm before the repo goes public.

```text
Phase 9: Release v1.0. Read CLAUDE.md, docs/brief.md and docs/progress.md first, then tell me in two lines what you'll do and what the gate is. Follow every rule in CLAUDE.md.

1. README: what the platform does; an architecture diagram in Mermaid; setup with docker compose up; how to run the recorder, a backtest and the dashboard; a results summary linking docs/results.md; screenshots; the credits text from the brief; and the paper-trading disclaimer.
2. MIT licence.
3. Before anything goes public, scan the whole git history for secrets, server addresses and vendor data, and fix anything found.
4. Fresh-clone test: clone into a new folder on my laptop and run everything by following only the README.
5. Ask me to confirm, then make the repo public and tag v1.0.

Gate: the fresh clone runs end to end with one command, and the scan of the whole history is clean.

When you're done: commit and push, and update docs/progress.md with the gate result and its evidence. Then report to me in plain words, including anything I must do by hand.
```

## Prompt 10: Challenger models (Phase 10, after v1.0)

Runs in week 5, then keeps the results fresh for as long as the experiment runs.

```text
Phase 10: Challenger models. Read CLAUDE.md, docs/brief.md and docs/progress.md first, then tell me in two lines what you'll build and what the gate is. Follow every rule in CLAUDE.md.

1. Heston and Bates calibration, ported from my btc-vol-models repo, with calibration diagnostics.
2. EWMA and GARCH realised-vol forecasts as baselines for the physical measure.
3. Out-of-sample comparison with the Black-76 spline surface. Promote Heston only if its out-of-sample pricing error is lower than the spline fit's and its calibration is stable. Promote Bates only if it beats Heston on short-dated wings out of sample.
4. A monthly job that re-runs the backtests on the growing data and updates docs/results.md.

Gate: each challenger is promoted or rejected, with the evidence recorded.

When you're done: run every test, commit and push, and update docs/progress.md with the gate result and its evidence. Then report to me in plain words, including anything I must do by hand.
```
