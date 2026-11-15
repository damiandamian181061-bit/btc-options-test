"""Cloudflare R2 through rclone, with the remote built from settings each time it runs.

The remote ``r2`` exists only in the environment of each rclone process, from ``R2_ACCESS_KEY_ID``,
``R2_SECRET_ACCESS_KEY``, ``R2_ENDPOINT`` and ``R2_BUCKET``. No rclone config file is ever written
or read (``--config ""``), nothing secret goes on a command line, and every message rclone prints
is scrubbed of the keys and the endpoint (which contains the Cloudflare account ID) before it is
logged or raised.

The R2 token has Object Read & Write on one bucket only, so ``no_check_bucket`` is set:
rclone must not try to create or inspect the bucket (Cloudflare's rclone guide).

Bucket layout: ``raw/`` holds the raw stream (kept 18 months, by an R2 lifecycle rule),
``exports/`` the database exports (kept for good).

R2 only ever grows from here. ``Rclone.run`` accepts four rclone commands and refuses every
other: ``copy`` (uploads and downloads), ``check``, ``cat`` and ``lsf`` (reads). So no
``sync``, ``delete``, ``purge`` or ``move`` can ever run against the bucket. Every upload also
carries ``--immutable --checksum``: a file already in R2 with the same size and hash is
skipped, and an upload that would change one fails instead. (``copyto`` is not allowed: rclone
does not apply ``--immutable`` to it, so even manifests go up with ``copy``.) Files are only
ever removed on the server (after 7 days, once found in R2) and, for raw files, by the
lifecycle rule after 18 months.
"""

import logging
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from optlab.config import Settings

logger = logging.getLogger(__name__)

REMOTE = "r2"
RAW_PREFIX = "raw"
EXPORTS_PREFIX = "exports/v1"
TIMEOUT_S = 3600
ALLOWED_COMMANDS = frozenset({"copy", "check", "cat", "lsf"})
UPLOAD_FLAGS = ("--immutable", "--checksum")


class RcloneError(RuntimeError):
    """rclone failed; the message is already scrubbed of secrets."""


class R2NotConfiguredError(RuntimeError):
    """One of the four R2_ settings is missing."""


@dataclass(frozen=True)
class RemoteConfig:
    """How rclone reaches the bucket: ``RCLONE_CONFIG_R2_*`` variables plus the bucket name."""

    bucket: str
    env: Mapping[str, str] = field(repr=False)
    secrets: tuple[str, ...] = field(default=(), repr=False)

    def target(self, path: str = "") -> str:
        return f"{REMOTE}:{self.bucket}/{path}".rstrip("/")


def r2_config(settings: Settings) -> RemoteConfig:
    """The R2 remote from settings; refuses unless all four R2_ variables are set."""
    key_s, secret_s, endpoint_s = (
        settings.r2_access_key_id,
        settings.r2_secret_access_key,
        settings.r2_endpoint,
    )
    if key_s is None or secret_s is None or endpoint_s is None or not settings.r2_bucket:
        missing = [
            name
            for name, value in (
                ("R2_ACCESS_KEY_ID", key_s),
                ("R2_SECRET_ACCESS_KEY", secret_s),
                ("R2_ENDPOINT", endpoint_s),
                ("R2_BUCKET", settings.r2_bucket),
            )
            if not value
        ]
        raise R2NotConfiguredError(f"R2 is not configured: {', '.join(missing)} not set")
    key, secret = key_s.get_secret_value(), secret_s.get_secret_value()
    endpoint = endpoint_s.get_secret_value()
    host = endpoint.split("://", 1)[-1].split("/", 1)[0]
    return RemoteConfig(
        bucket=str(settings.r2_bucket),
        env={
            "RCLONE_CONFIG_R2_TYPE": "s3",
            "RCLONE_CONFIG_R2_PROVIDER": "Cloudflare",
            "RCLONE_CONFIG_R2_ACCESS_KEY_ID": key,
            "RCLONE_CONFIG_R2_SECRET_ACCESS_KEY": secret,
            "RCLONE_CONFIG_R2_ENDPOINT": endpoint,
            "RCLONE_CONFIG_R2_ACL": "private",
            "RCLONE_CONFIG_R2_NO_CHECK_BUCKET": "true",
        },
        secrets=tuple(s for s in (secret, key, endpoint, host, host.split(".", 1)[0]) if s),
    )


Runner = Callable[..., subprocess.CompletedProcess[str]]


class Rclone:
    """The few rclone operations the platform needs, always against ``config``."""

    def __init__(
        self,
        config: RemoteConfig,
        *,
        binary: str | None = None,
        runner: Runner = subprocess.run,
    ) -> None:
        self.config = config
        self.binary = binary or os.environ.get("OPTLAB_RCLONE", "rclone")
        self._runner = runner

    def scrub(self, text: str) -> str:
        for secret in sorted(self.config.secrets, key=len, reverse=True):
            text = text.replace(secret, "[redacted]")
        return text

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "TZ", "SSL_CERT_FILE")}
        env.update(self.config.env)
        env["HOME"] = tempfile.gettempdir()
        return env

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        if not args or args[0] not in ALLOWED_COMMANDS:
            raise ValueError(f"rclone {args[:1]} is not allowed: only {sorted(ALLOWED_COMMANDS)}")
        uploading = args[0] == "copy" and args[-1].startswith(f"{REMOTE}:")
        if uploading and not all(flag in args for flag in UPLOAD_FLAGS):
            raise ValueError("uploads to R2 must use --immutable --checksum: objects never change")
        command = [self.binary, "--config", "", *args]
        try:
            result = self._runner(
                command,
                env=self._env(),
                capture_output=True,
                text=True,
                timeout=TIMEOUT_S,
                check=False,
            )
        except FileNotFoundError:
            raise RcloneError(
                f"rclone not found ({self.binary}): install it (Ubuntu: sudo apt install rclone)"
            ) from None
        if check and result.returncode != 0:
            raise RcloneError(
                f"rclone {args[0]} failed (exit {result.returncode}): "
                + self.scrub(result.stderr.strip())[-2000:]
            )
        return result

    # ----------------------------------------------------------------- operations

    def _with_list(self, files: Sequence[str], *args: str) -> subprocess.CompletedProcess[str]:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8") as listing:
            listing.write("\n".join(files) + "\n")
            listing.flush()
            return self.run(*args[:1], "--files-from-raw", listing.name, *args[1:])

    def upload(self, local_root: Path, prefix: str, files: Sequence[str]) -> None:
        """Copy ``files`` (paths relative to ``local_root``) to ``prefix`` in the bucket."""
        if files:
            self._with_list(
                files,
                "copy",
                *UPLOAD_FLAGS,
                "--no-traverse",
                str(local_root),
                self.config.target(prefix),
            )

    def verify(self, local_root: Path, prefix: str, files: Sequence[str]) -> None:
        """Raise unless every file exists in the bucket with the same size and MD5."""
        if files:
            self._with_list(
                files, "check", "--one-way", str(local_root), self.config.target(prefix)
            )

    def download(self, prefix: str, local_root: Path, files: Sequence[str]) -> None:
        if files:
            local_root.mkdir(parents=True, exist_ok=True)
            self._with_list(
                files, "copy", "--no-traverse", self.config.target(prefix), str(local_root)
            )

    def put(self, local_file: Path, path: str) -> None:
        """Upload one file to ``path``; its name must stay the same (``copy``, not ``copyto``)."""
        folder, _, name = path.rpartition("/")
        if name != local_file.name:
            raise ValueError(f"put keeps the file name: {local_file.name} is not {name}")
        self.upload(local_file.parent, folder, [name])

    def get(self, path: str) -> bytes | None:
        """A small object's bytes (manifests), or None if it isn't listed in the bucket.

        Whether the object exists is decided by listing its folder (``lsf``) first, never by
        what ``cat`` prints: on R2, as on any S3 store, ``cat`` of a missing key exits 0 and
        prints nothing. ``cat`` runs only for a listed object, and one that reads back empty
        is an error.
        """
        folder, _, name = path.rpartition("/")
        if name not in self.names(folder):
            return None
        data = self.run("cat", self.config.target(path)).stdout.encode()
        if not data:
            raise RcloneError(f"rclone cat printed nothing for {path}, which is listed in R2")
        return data

    def names(self, prefix: str) -> list[str]:
        """File names directly under ``prefix`` (empty if it doesn't exist)."""
        result = self.run("lsf", "--files-only", self.config.target(prefix), check=False)
        if result.returncode != 0:
            if "not found" in result.stderr:
                return []
            raise RcloneError(f"rclone lsf failed: {self.scrub(result.stderr.strip())[-2000:]}")
        return sorted(line for line in result.stdout.splitlines() if line)
