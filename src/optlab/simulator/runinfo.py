"""What is saved with every run so it can be reproduced: config, git commit, dataset, seed."""

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from optlab.domain.clock import ms_to_datetime
from optlab.simulator.config import BacktestConfig


class VersionedSource(Protocol):
    def dataset_versions(self, first: date, last: date) -> dict[date, str]: ...


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    config_hash: str
    git_commit: str
    git_dirty: bool
    dataset_hash: str
    dataset_versions: dict[str, str]  # day -> the export's dataset version
    seed: int
    started_ms: int


def config_hash(config: BacktestConfig) -> str:
    text = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def git_state(repo: Path = Path()) -> tuple[str, bool]:
    """The commit checked out and whether tracked files differ from it ("unknown" outside git)."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],  # noqa: S607
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout  # fmt: skip
    except (OSError, subprocess.CalledProcessError):
        return "unknown", True
    return commit, bool(status.strip())


def dataset_hash(versions: dict[date, str]) -> str:
    """SHA-256 over every day's dataset version, in day order."""
    text = "".join(f"{d.isoformat()} {v}\n" for d, v in sorted(versions.items()))
    return hashlib.sha256(text.encode()).hexdigest()


def run_info(config: BacktestConfig, source: VersionedSource, repo: Path = Path()) -> RunInfo:
    first = ms_to_datetime(config.start_ms).date()
    last = ms_to_datetime(config.end_ms - 1).date()
    versions = source.dataset_versions(first, last)
    commit, dirty = git_state(repo)
    chash = config_hash(config)
    started = time.time_ns() // 1_000_000
    run_id = f"{ms_to_datetime(started):%Y%m%dT%H%M%SZ}-{config.name}-{chash[:6]}"
    return RunInfo(
        run_id=run_id,
        config_hash=chash,
        git_commit=commit,
        git_dirty=dirty,
        dataset_hash=dataset_hash(versions),
        dataset_versions={d.isoformat(): v for d, v in sorted(versions.items())},
        seed=config.seed,
        started_ms=started,
    )
