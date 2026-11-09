# Server setup: recorder-1

How to build the always-on server from nothing, in the order it was done on 9 November 2026
(Phase 0). Every command below was run on the real server; the outputs quoted are what a correct
setup prints.

`recorder-1` runs the recorder, paper trading and the dashboard. Everything else (tests,
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
There are **no automatic reboots**, so a reboot never interrupts the recorder unannounced. When
`/var/run/reboot-required` exists, reboot by hand at a quiet time. Docker comes from Docker's own
repository and is **not** auto-updated, because a Docker upgrade restarts every container. Update
it by hand with `sudo apt-get update && sudo apt-get install --only-upgrade docker-ce
docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin`.

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

Add the **public** half to the repo as a read-only deploy key. From the laptop, with `gh` signed in:

```bash
ssh deploy@SERVER_IP 'cat ~/.ssh/github_btc-options-lab.pub' \
  | gh repo deploy-key add - --title "recorder-1 (read-only)" -R damiandamian181061-bit/btc-options-lab
gh repo deploy-key list -R damiandamian181061-bit/btc-options-lab    # shows read-only
```

(Or on github.com: the repo's **Settings → Deploy keys → Add deploy key**, with **Allow write
access unticked**.)

Then clone, on the server:

```bash
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

## If you are locked out

1. **Droplet Console** (Droplet page → **Access → Launch Droplet Console**). It runs through
   `droplet-agent` and SSH, so with root login off, change the login user to **`deploy`**.
2. **Recovery Console** (Droplet page → **Access → Recovery Console**) works even if SSH itself
   is broken. It needs a root password: use **Reset root password** on the same page first (the
   password is shown or emailed once; put it in the password manager). Local console logins are
   not affected by the SSH rules.
3. Once in, undo the last change. For SSH, that means
   `rm /etc/ssh/sshd_config.d/10-hardening.conf && systemctl reload ssh`.
