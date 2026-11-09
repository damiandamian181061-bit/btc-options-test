# Later

New ideas go here, not into the current phase. Each entry says where it came from and which phase
could pick it up.

- **Server setup as a script** (Phase 0). Turn `infra/server-setup.md` into an idempotent script,
  so a rebuild is one command after the Droplet exists. Phase 9 at the latest.
- **Reboot policy for kernel updates** (Phase 0). Security updates install automatically, but
  nothing reboots on its own, so new kernels wait. Options: a weekly scheduled reboot at a quiet
  hour (the recorder restarts by itself in under 5 minutes), or a healthchecks alert when
  `/var/run/reboot-required` exists. Phase 1 or 2, alongside the disk check.
- **DigitalOcean metrics agent** (Phase 0). `do-agent` wasn't installed on recorder-1 even though
  monitoring was ticked. Damian may install it from the Droplet's Graphs tab; it doesn't affect the
  platform.
