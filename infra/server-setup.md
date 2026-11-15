# Server setup: recorder-1

How to build the always-on server from nothing, in the order it was done on 9 November 2026
(Phase 0). Every command below was run on the real server; the outputs quoted are what a correct
setup prints.

`recorder-1` runs the recorder, paper trading (the rough paper service from Phase 2B) and the
dashboard. Everything else (tests,
backtests) runs on the laptop.

> **Placeholders.** `SERVER_IP` is the Droplet's public IPv4 address. Never write the real
> address, the R2 endpoint or the Cloudflare account ID into this file or anywhere in the repo.
> The gitleaks rules in `.gitleaks.toml` block public IP addresses, R2 endpoints and healthchecks
> ping URLs from being committed.

## 1. Create the Droplet (DigitalOcean control panel)

You need a DigitalOcean account with two-step login on, and the laptop's SSH public key
(`~/.ssh/id_ed25519.pub`, named `damian-laptop`).

1. **Create → Droplets.**
2. Region: **London (LON1)**.
3. Image: **Ubuntu 26.04 (LTS) x64**.
4. Size: **Basic**, CPU option **Regular**, **2 vCPU / 4 GB RAM / 80 GB SSD** ($24 a month plus
   VAT in October 2026).
5. Authentication: **SSH Key**, tick `damian-laptop`. Paste it first under **Add an SSH Key** if
   it isn't listed.
6. **Public IPv4 on, IPv6 off.** Monitoring (Improved Metrics) **on**. Backups **off** (data is
   backed up to Cloudflare R2 instead).
7. Hostname: **`recorder-1`**. Create.
8. Note the public IPv4 address in your own notes (not in the repo).

> On 9 November 2026 the metrics agent (`do-agent`) was found **not installed**: only
> `droplet-agent` (the Web Console agent) was there. To get the graphs, install it from the
> Droplet's **Graphs** tab, or follow DigitalOcean's "Install the metrics agent" guide.

## 2. Cloud firewall `ssh-only` (control panel)

**Networking → Firewalls → Create Firewall**:

- Name: `ssh-only`.
- Inbound: one rule, **SSH, TCP 22, all IPv4 and IPv6 sources**. The Web Console also connects over
  port 22, so don't restrict the sources.
- Outbound: leave the defaults (all traffic allowed).
- Apply to the Droplet `recorder-1`.

This is the server's only firewall. ufw inside the server stays **off** (decided 9 November).
The cloud firewall sits outside the server and Docker can't bypass it, whereas Docker does bypass
ufw for published container ports. Containers must never publish ports to the internet; the
public dashboard goes out through a Cloudflare Tunnel (Phase 6), which connects outwards.

If you ever turn ufw on, allow SSH first: `sudo ufw allow 22/tcp`, then `sudo ufw enable`.

## 3. First login as root and checks

From the laptop (WSL), with the SSH key loaded (`ssh-add -l` lists `damian-laptop`):

```bash
ssh root@SERVER_IP      # first time: check the host key, type yes
```

Read-only checks before changing anything:

```bash
. /etc/os-release; echo "$PRETTY_NAME, kernel $(uname -r)"
df -h /; free -h; swapon --show
timedatectl
sshd -T | grep -E '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication) '
ssh-keygen -lf /root/.ssh/authorized_keys   # must show the damian-laptop key's fingerprint
ufw status
apt list --upgradable | wc -l; ls /var/run/reboot-required
```

What a fresh Droplet looked like: UTC already set, chrony syncing time, password logins already
off, root login on, ufw inactive, no swap, unattended-upgrades enabled, updates waiting with a
reboot required.

## 4. The `deploy` user (as root)

`deploy` has the laptop key, no password at all, and passwordless sudo. It is also in the `docker`
group, which is root-equivalent anyway, so a sudo password would add little protection.
The SSH key, which has a passphrase, is the protection.

```bash
useradd --create-home --shell /bin/bash --comment "deploy user" deploy
usermod -p '*' deploy                    # no password: can't log in with one, but not "locked"
usermod -aG sudo,adm deploy              # adm = can read system logs
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
install -m 600 -o deploy -g deploy /root/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys

tmp=$(mktemp)
echo 'deploy ALL=(ALL:ALL) NOPASSWD: ALL' > "$tmp"
visudo -cf "$tmp" && install -m 440 -o root -g root "$tmp" /etc/sudoers.d/deploy
rm -f "$tmp"; visudo -c
```

Ubuntu 26.04 uses `sudo-rs`; the rule above works with it.

**Before going further, prove the new login works** from the laptop, in a new terminal:

```bash
ssh deploy@SERVER_IP 'whoami; id -nG; sudo -n true && echo "passwordless sudo: OK"'
```

## 5. SSH: keys only, no root login

From here on, work as `deploy` (`ssh deploy@SERVER_IP`). Keep that session open while changing
SSH, as a way back in if something goes wrong.

sshd keeps the **first** value it reads for each setting, and the drop-in folder is read before the
main config. So the file is named `10-…` to sort ahead of `50-cloud-init.conf`:

```bash
sudo tee /etc/ssh/sshd_config.d/10-hardening.conf >/dev/null <<'CONF'
# btc-options-lab: SSH keys only, no root login.
# Named 10-* so it is read before 50-cloud-init.conf; sshd keeps the first value it sees.
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes
AuthenticationMethods publickey
X11Forwarding no
CONF
sudo sshd -t && sudo systemctl reload ssh      # only reload if the config check passes
sudo sshd -T | grep -E '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication|authenticationmethods) '
```

Check from the laptop, with new connections:

```bash
ssh root@SERVER_IP true
#   root@SERVER_IP: Permission denied (publickey).        <- root refused
ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password deploy@SERVER_IP true
#   deploy@SERVER_IP: Permission denied (publickey).      <- passwords refused, keys only
ssh deploy@SERVER_IP 'echo OK'                            # <- prints OK
```

## 6. Automatic security updates

Ubuntu 26.04 on DigitalOcean ships with unattended-upgrades already on. Check it:

```bash
systemctl is-active unattended-upgrades
apt-config dump | grep -E 'APT::Periodic::(Update-Package-Lists|Unattended-Upgrade) '
#   APT::Periodic::Update-Package-Lists "1";
#   APT::Periodic::Unattended-Upgrade "1";
```

If either line is missing: `sudo apt-get install -y unattended-upgrades` and
`sudo dpkg-reconfigure -plow unattended-upgrades` (answer Yes).

Defaults kept: security updates install daily from `${distro_codename}-security` (plus ESM).
Docker comes from Docker's own repository and is **not** auto-updated, because a Docker upgrade
restarts every container. Update it by hand with `sudo apt-get update && sudo apt-get install
--only-upgrade docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin`.

**Reboot policy (decided 15 Nov 2026).** When an automatic update needs a restart
(`/var/run/reboot-required` exists after an unattended-upgrades run), the server restarts by
itself at the next **04:00 UTC**, even with someone logged in. It never restarts otherwise. The
setting is `infra/apt/52optlab-reboot`, which sorts after Ubuntu's `50unattended-upgrades`, so
its values win:

```bash
sudo install -m 644 -o root -g root infra/apt/52optlab-reboot /etc/apt/apt.conf.d/52optlab-reboot
apt-config dump | grep -E '^Unattended-Upgrade::Automatic-Reboot'
#   Unattended-Upgrade::Automatic-Reboot "true";
#   Unattended-Upgrade::Automatic-Reboot-Time "04:00";
#   Unattended-Upgrade::Automatic-Reboot-WithUsers "true";
systemctl is-enabled docker.service containerd.service    # both "enabled": Docker starts at boot
```

The updates themselves install from `apt-daily-upgrade.timer` (about 06:00 to 07:00 UTC), and a
restart they need waits for the next 04:00. unattended-upgrades then runs `shutdown -r 04:00`,
which `shutdown --show` lists while it is pending (`sudo shutdown -c` cancels it). After the
restart:

- Docker starts at boot, and every service (`restart: unless-stopped`) starts again by itself;
- the recorder misses a minute or two;
- the jobs service records the boot time on its start-up run (`host_boots`), and the hourly
  data checks label the missing minutes `server_reboot` next to `missing_snapshot`, so the gap
  report says why they are missing.

Nothing else runs at 04:00: the backup is at 00:30, the data checks at :05 past each hour, and
the monthly restore test at 03:00 to about 03:10 on the 1st. To see whether a restart is
waiting: `ls /var/run/reboot-required && cat /var/run/reboot-required.pkgs`.

## 7. Time: UTC with time sync

Already correct on a fresh Droplet. Ubuntu 26.04 uses **chrony** (with NTS) instead of
systemd-timesyncd. Check, and fix if needed:

```bash
timedatectl            # Time zone: Etc/UTC; System clock synchronized: yes; NTP service: active
chronyc tracking       # Leap status: Normal
sudo timedatectl set-timezone Etc/UTC   # only if not already UTC
sudo timedatectl set-ntp true           # only if NTP service is inactive
```

## 8. Docker Engine and the Compose plugin, with log rotation

From Docker's official apt repository, with its signing key checked against Docker's published
fingerprint `9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88`. The log-rotation config is written
**before** installing, so Docker starts with it: each container keeps at most 5 compressed log files
of 10 MB.

```bash
APT="sudo env DEBIAN_FRONTEND=noninteractive apt-get -y"
$APT update
$APT install ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
gpg --show-keys --with-colons /etc/apt/keyrings/docker.asc | awk -F: '/^fpr/{print $10; exit}'
#   must print 9DC858229FC7DD38854AE2D88D81803C0EBFCD88, otherwise stop

. /etc/os-release
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<SRC
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Signed-By: /etc/apt/keyrings/docker.asc
SRC

sudo install -m 0755 -d /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "5",
    "compress": "true"
  }
}
JSON

$APT update
$APT install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker deploy      # takes effect at deploy's next login
```

Installed on 9 November 2026: Docker 29.9.3, Docker Compose v5.7.0.

## 9. Swap: 2 GB

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
grep -qE '^/swapfile\s' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swappiness.conf
sudo sysctl -p /etc/sysctl.d/99-swappiness.conf
swapon --show
```

Swappiness 10 means swap is used only when memory is really short. The database works better
in RAM.

## 10. Install waiting updates and reboot

`full-upgrade` updates packages within Ubuntu 26.04 (it is not a release upgrade) and lets new
kernels install. `--force-confold` keeps existing config files.

```bash
APT="sudo env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"
$APT update && $APT full-upgrade && $APT autoremove
sudo reboot
```

After about a minute, log in again as `deploy` and run the checks in step 13. The reboot also
proves that swap, Docker and the SSH rules survive a restart.

## 11. Read-only deploy key and clone

The server pulls the repo with its own SSH key, which can only read this one repository. The
private half never leaves the server. It has no passphrase because the server has to pull
unattended; that is normal for a deploy key.

On the server, as `deploy`:

```bash
ssh-keygen -t ed25519 -N "" -C "recorder-1 read-only deploy key for btc-options-lab" \
  -f ~/.ssh/github_btc-options-lab
cat >> ~/.ssh/config <<'CONF'
Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/github_btc-options-lab
    IdentitiesOnly yes
CONF
chmod 600 ~/.ssh/config

# Trust GitHub's SSH host keys, fetched over HTTPS from GitHub's API.
python3 -c 'import json, urllib.request; d = json.load(urllib.request.urlopen("https://api.github.com/meta")); print("\n".join("github.com " + k for k in d["ssh_keys"]))' >> ~/.ssh/known_hosts
ssh-keygen -lf ~/.ssh/known_hosts
#   must include SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU github.com (ED25519)
```

Add the **public** half to the repo as a read-only deploy key. From the laptop, with `gh` signed in
(`gh auth login --hostname github.com --git-protocol https --web --scopes workflow`):

```bash
ssh deploy@SERVER_IP 'cat ~/.ssh/github_btc-options-lab.pub' > /tmp/recorder-1-deploy-key.pub
gh repo deploy-key add /tmp/recorder-1-deploy-key.pub --title "recorder-1 (read-only)" \
  -R damiandamian181061-bit/btc-options-lab           # no --allow-write: read-only
gh repo deploy-key list -R damiandamian181061-bit/btc-options-lab    # shows read-only
```

Or do it on github.com: the repo's **Settings → Deploy keys → Add deploy key**, with **Allow write
access unticked**. A key added through `gh` is tied to gh's sign-in token: if that token is ever
revoked, GitHub deletes the deploy key too, and `git pull` on the server fails. Running services
are not affected. If that happens, re-add the same public key, by either method. A key added on
github.com has no such link.

Check that GitHub accepts the key, then clone, on the server. `</dev/null` stops `ssh` from
swallowing the rest of a pasted script:

```bash
ssh -T git@github.com </dev/null
#   Hi damiandamian181061-bit/btc-options-lab! You've successfully authenticated, but GitHub does not provide shell access.
sudo install -d -o deploy -g deploy -m 755 /srv/btc-options-lab
git clone git@github.com:damiandamian181061-bit/btc-options-lab.git /srv/btc-options-lab
```

To update later: `git -C /srv/btc-options-lab pull --ff-only`.

## 12. The server's `.env` (by hand)

```bash
cd /srv/btc-options-lab
cp .env.example .env
chmod 600 .env          # readable by deploy only
nano .env               # paste the values from the password manager, save with Ctrl+O, exit with Ctrl+X
```

Only Damian fills in `.env`. Claude Code never reads it, and it is never committed (`.gitignore`, a
pre-commit hook and gitleaks all block it). Which values each phase needs is in `docs/brief.md`
(Details and secrets tables).

## 13. Verify (the Phase 0 gate)

From the laptop, with new connections:

```bash
ssh root@SERVER_IP true                         # Permission denied (publickey).
ssh deploy@SERVER_IP 'docker run --rm hello-world | grep "Hello from Docker"'
ssh deploy@SERVER_IP 'bash -s' <<'EOF'
sudo sshd -T | grep -E '^(permitrootlogin|passwordauthentication|authenticationmethods) '
swapon --show; cat /proc/sys/vm/swappiness
systemctl is-active docker unattended-upgrades chrony droplet-agent
docker info --format '{{.LoggingDriver}}'; cat /etc/docker/daemon.json
timedatectl | grep -E 'Time zone|synchronized'
apt list --upgradable 2>/dev/null | tail -n +2 | wc -l; ls /var/run/reboot-required 2>/dev/null
git -C /srv/btc-options-lab remote -v; ls -l /srv/btc-options-lab/.env
EOF
```

## 14. The recorder (Phase 1)

Everything runs in Docker Compose from `/srv/btc-options-lab`, always through `infra/dc`,
which adds `--env-file .env` and refuses `config` (that would print the database password).
Three services, none publishing a port: `db` (TimescaleDB), `recorder` and `health`. Each has
`restart: unless-stopped`, so it comes back after a crash or a reboot.

**Before the first start**, `.env` needs `HC_PING_URL_RECORDER` and `HC_PING_URL_DISK` (pasted
by Damian) and `DATABASE_URL`. The database password is generated on the server and written
straight into `.env`, never shown. The command prints only a count that confirms it worked:

```bash
cd /srv/btc-options-lab
git pull --ff-only
# Only if the DATABASE_URL line is still empty: a 64-hex-character password, never printed.
grep -q '^DATABASE_URL=$' .env && \
  sed -i "s|^DATABASE_URL=\$|DATABASE_URL=postgresql://optlab:$(openssl rand -hex 32)@db:5432/optlab|" .env
grep -c '^DATABASE_URL=postgresql://optlab:[0-9a-f]\{64\}@db:5432/optlab$' .env   # must print 1
ls -l .env                                                                         # still -rw------- deploy
mkdir -p data/raw                                                                  # owned by deploy (uid 1000)
```

The user and database are both `optlab`, and the password only matters the first time `db`
starts, when it creates its data directory. Changing it later needs `ALTER USER` as well.

**First start.** The database is new and empty, so there is nothing to back up yet:

```bash
infra/dc build                                   # the optlab image (about 2 minutes)
infra/dc up -d db                                # TimescaleDB; wait for "healthy" in: infra/dc ps
infra/dc run --rm recorder alembic -c infra/alembic.ini upgrade head
infra/dc up -d                                   # recorder and health
infra/dc ps                                      # three services Up; db (healthy)
infra/dc logs --tail 30 recorder                 # "discovery: ... options", then "status:" lines each minute
```

**Every later migration** needs a backup first (CLAUDE.md). Until the nightly R2 backups exist
(Phase 2), that is a local dump:

```bash
mkdir -p backups
infra/dc exec -T db pg_dump -U optlab -d optlab -Fc > "backups/pre-migration-$(date -u +%Y%m%dT%H%MZ).dump"
ls -lh backups/                                  # the dump exists and is not empty
infra/dc run --rm recorder alembic -c infra/alembic.ini upgrade head
```

**Updating the code** (no schema change): `git pull --ff-only && infra/dc up -d --build`. The
recorder restarts in a few seconds; its snapshot for that minute may be missed, which the
gap check tolerates (up to 5 minutes).

**Checking it.** The recorder logs a `status:` line every minute (connections up, messages per
second, rows in the last snapshot, database backlog). The two gate queries run inside the `db`
container, where `psql` needs no password:

```bash
infra/dc exec -T db psql -U optlab -d optlab < infra/sql/gap_check.sql
infra/dc exec -T db psql -U optlab -d optlab < infra/sql/volume.sql
du -sh data/raw/quotes data/raw/trades
```

**Raw files** go to `data/raw/{quotes,trades}/YYYY-MM-DD/`, one zstd Parquet file per hour (and
per recorder run). A name ending `.inprogress` is the hour being written; `.incomplete` is an
hour cut short by a crash, which cannot be read.

**Health.** The `health` service pings `HC_PING_URL_RECORDER` every minute while `option_quotes`
has rows from the last 2 minutes, and `HC_PING_URL_DISK` every 10 minutes with how full the disk
is (failure pings marked WARNING at 70% and CRITICAL at 85%).

## 15. Data platform: backups, checks, restore test, status page (Phase 2)

Two more services run from the same image: `jobs` (`optlab-data scheduler`) and `dashboard`
(the Streamlit status page). `jobs` is the only service that gets the four `R2_` variables and
`HC_PING_URL_BACKUP`; its rclone remote exists only in the environment of each rclone process,
built from those variables, with no config file (`--config ""`). Never run `infra/dc config`,
`docker inspect`, `env`, `printenv` or `rclone config show` on the server: each would print
secrets.

**What `jobs` does** (UTC):

- every hour at :05, the data checks of every finished hour not yet checked, into
  `data_quality` and `data_quality_runs`;
- at 00:30, the backup: data checks first, then every finished day not yet verified in R2 is
  exported to `data/exports/v1/` (zstd Parquet + manifest), its raw files and export uploaded,
  every file checked against R2 (size and MD5), the manifests uploaded last, and the day
  recorded in `dataset_exports`. Then the server's raw and export files of days more than 7
  days old are removed, but only files listed in a manifest and found again in R2 just
  before. A success pings `HC_PING_URL_BACKUP`; a failure sends a failure ping and the
  backup is retried every hour.
- On start-up it runs both once, so a restart catches up.

**R2 layout** (bucket `btc-options-backup`): `raw/quotes/DAY/`, `raw/trades/DAY/` and
`raw/manifests/DAY.json` for the raw stream (kept 18 months); `exports/v1/TABLE/date=DAY/...`
and `exports/v1/manifests/DAY.json` for the database exports (kept for good). The R2 token has
Object Read & Write only, so it cannot set lifecycle rules: the 18-month rule is added by hand
in the Cloudflare dashboard (R2, the bucket, Settings, Object lifecycle rules): prefix `raw/`,
delete objects 548 days after upload.

**Deploy** (from `/srv/btc-options-lab`; migration `0002` adds tables, so back up first):

```bash
git pull --ff-only
infra/dc build                                          # the image, now with rclone and Streamlit
mkdir -p backups
infra/dc exec -T db pg_dump -U optlab -d optlab -Fc </dev/null > "backups/pre-migration-$(date -u +%Y%m%dT%H%MZ).dump"
ls -lh backups/                                         # the dump exists and is not empty
infra/dc run --rm recorder alembic -c infra/alembic.ini upgrade head </dev/null
infra/dc up -d recorder health dashboard               # just after a minute boundary
infra/dc run --rm jobs optlab-data backup </dev/null   # first backup by hand, then:
infra/dc up -d jobs
```

**Gap report and checks by hand:**

```bash
infra/dc run --rm jobs optlab-data gap-report </dev/null
infra/dc run --rm jobs optlab-data checks --hour 2026-11-13T21:00 </dev/null   # re-check an hour
```

**Restore test.** `infra/restore-test [DAY]` starts its own Compose project,
`optlab-restore-test` (`infra/restore-test.compose.yaml`): a throwaway TimescaleDB with its own
volume and a 1 GB memory limit, plus the restore job. It refuses any database that already
has tables, downloads the day from R2, checks every file's SHA-256, loads it, and compares
each table's row count and checksum with the manifest. On exit it removes that project's
containers, network and volume, and nothing else. It runs monthly through a systemd timer:

```bash
infra/restore-test 2026-11-13                           # by hand: prints PASS or FAIL per table
sudo cp infra/systemd/optlab-restore-test.service infra/systemd/optlab-restore-test.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now optlab-restore-test.timer
systemctl list-timers optlab-restore-test.timer         # next run: the 1st of the month, 03:00 UTC
journalctl -u optlab-restore-test.service               # the last run's report
```

**Status page.** `dashboard` listens on the server's 127.0.0.1:8501 only. From the laptop:

```bash
ssh -N -L 8501:127.0.0.1:8501 deploy@SERVER_IP          # leave running, then open http://localhost:8501
```

Its database connection is read-only (`default_transaction_read_only`), so the page cannot
change anything.

**On the laptop**, `uv run optlab-data sync 2026-11-13 2026-11-14` (or `--from`/`--to`,
`--list`, `--raw`) pulls exported days into `data/exports/v1/` for backtests. It needs
`rclone` (`sudo apt install rclone`) and the four `R2_` lines in the laptop's own `.env`.

## 16. Rough paper service (Phase 2B)

`paper` (`optlab-paper-rough`) runs the ROUGH credit-spread rule on the live recorded data, with
paper fills only (docs/rough-version.md). It gets only `DATABASE_URL` and the three safety
switches, no keys, and no port. It has a 512 MB memory limit and `restart: unless-stopped`. Its
decisions, spreads, trades and equity are on the status page's "Paper trading (ROUGH)" tab.

**Every migration also needs the recorder restarted on the new image.** The recorder refuses
to start on any schema revision but its own. If it kept running an old image, its next restart
(a crash or a 04:00 reboot) would fail. Restart only the services whose code changed, plus the
recorder after a migration. Do it just after a minute boundary (the recorder writes each minute's
snapshot a second or two after the minute), and always with `--no-deps`, so `db` is never
recreated. Avoid 07:55 to 08:35 UTC (expiry, discovery and delivery prices), :05 past each hour
(data checks) and 00:30 (backup). If a deploy could run into 00:30, stop `jobs` first and start
it again afterwards.

**Deploy of migration `0003`** (from `/srv/btc-options-lab`):

```bash
git pull --ff-only
infra/dc build                                          # the image; running services keep the old one
mkdir -p backups
DUMP="backups/pre-migration-$(date -u +%Y%m%dT%H%MZ).dump"
infra/dc exec -T db pg_dump -U optlab -d optlab -Fc </dev/null > "$DUMP"
infra/dc exec -T db pg_restore --list < "$DUMP" | grep -c 'TABLE DATA'   # the dump reads back
infra/dc run --rm --no-deps recorder alembic -c infra/alembic.ini upgrade head </dev/null
# at hh:mm:02, just after a minute boundary:
infra/dc up -d --no-deps recorder jobs dashboard paper
infra/dc ps                                             # health keeps its old container
infra/dc logs --tail 20 paper                           # "ROUGH paper service ... starting"
```

`infra/dc run --rm --no-deps paper optlab-paper-rough --once </dev/null` processes the latest
snapshot once and prints what it did. With a decision already made that day, it changes nothing.

## If you are locked out

1. **Droplet Console** (Droplet page → **Access → Launch Droplet Console**). It runs through
   `droplet-agent` and SSH, so with root login off, change the login user to **`deploy`**.
2. **Recovery Console** (Droplet page → **Access → Recovery Console**) works even if SSH itself
   is broken. It needs a root password: use **Reset root password** on the same page first (the
   password is shown or emailed once; put it in the password manager). Local console logins are
   not affected by the SSH rules.
3. Once in, undo the last change. For SSH, that means
   `rm /etc/ssh/sshd_config.d/10-hardening.conf && systemctl reload ssh`.
