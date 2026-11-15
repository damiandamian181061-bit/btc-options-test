"""Static checks of the deployment files: restarts, ports, secrets, and the restore test."""

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
INFRA = REPO_ROOT / "infra"


def compose(name: str = "compose.yaml") -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((INFRA / name).read_text(encoding="utf-8"))
    return data


APPS = ("recorder", "health", "jobs", "dashboard", "paper")
R2_VARS = {"R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT", "R2_BUCKET"}


def test_services_and_restart_policy() -> None:
    services = compose()["services"]
    assert set(services) == {"db", *APPS}
    for name, service in services.items():
        assert service["restart"] == "unless-stopped", name
        assert "mem_limit" in service, name


def test_only_the_dashboard_publishes_a_port_and_only_on_localhost() -> None:
    for name, service in compose()["services"].items():
        assert "expose" not in service, name
        assert service.get("network_mode") != "host", name
        if name == "dashboard":
            assert service["ports"] == ["127.0.0.1:8501:8501"]
        else:
            assert "ports" not in service, name


def test_apps_get_only_the_settings_they_need() -> None:
    services = compose()["services"]
    env = {name: services[name]["environment"] for name in APPS}
    for name in APPS:
        assert env[name]["LIVE_TRADING"] == "${LIVE_TRADING:-false}"
        assert env[name]["DATABASE_URL"].startswith("${DATABASE_URL:?")
        assert not any(k.startswith(("DERIBIT_TESTNET", "CLOUDFLARE")) for k in env[name])
    secrets = {name: {k for k in env[name] if k.startswith(("HC_", "R2_"))} for name in APPS}
    assert secrets == {
        "recorder": set(),
        "health": {"HC_PING_URL_RECORDER", "HC_PING_URL_DISK"},
        "jobs": R2_VARS | {"HC_PING_URL_BACKUP"},
        "dashboard": set(),
        "paper": set(),  # the rough paper service needs no keys: it sends nothing anywhere
    }
    for service in services.values():
        assert "env_file" not in service  # never hand a container the whole .env


def test_images_are_pinned_and_apps_wait_for_the_database() -> None:
    services = compose()["services"]
    assert services["db"]["image"].startswith("timescale/timescaledb:2.")
    assert not services["db"]["image"].endswith(":latest")
    for name in APPS:
        assert services[name]["depends_on"]["db"]["condition"] == "service_healthy"


def test_rclone_comes_from_the_official_image_pinned_by_digest() -> None:
    dockerfile = (INFRA / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM rclone/rclone:1.75.2@sha256:" in dockerfile
    assert "COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone" in dockerfile
    assert "curl" not in dockerfile


def test_the_restore_test_never_touches_the_live_project() -> None:
    restore = compose("restore-test.compose.yaml")
    live = compose()
    assert restore["name"] == "optlab-restore-test" != live["name"]
    services = restore["services"]
    assert set(services) == {"restore-db", "restore"}
    assert set(restore["volumes"]) == {"restore-pgdata"}
    assert not set(restore["volumes"]) & set(live["volumes"])
    for name, service in services.items():
        assert "ports" not in service, name
        assert "mem_limit" in service, name
        assert "restart" not in service, name  # a one-off, never left running
        assert "env_file" not in service, name
    db = services["restore-db"]
    assert db["image"] == live["services"]["db"]["image"]
    assert db["volumes"] == ["restore-pgdata:/var/lib/postgresql/data"]
    job = services["restore"]["environment"]
    assert job["OPTLAB_RESTORE_TARGET"] == "throwaway"
    assert "@restore-db:5432/restore" in job["DATABASE_URL"]
    assert "${DATABASE_URL" not in str(services)  # the live database's URL is never used
    assert {k for k in job if k.startswith(("HC_", "R2_"))} == R2_VARS | {"HC_PING_URL_BACKUP"}


def test_restore_script_cleans_up_only_its_own_project() -> None:
    script = (INFRA / "restore-test").read_text(encoding="utf-8")
    assert (INFRA / "restore-test").stat().st_mode & 0o111
    assert "-f infra/restore-test.compose.yaml" in script
    assert "PROJECT=optlab-restore-test" in script
    assert 'docker compose -p "$PROJECT"' in script  # pinned, whatever the file says
    assert compose()["name"] != "optlab-restore-test"
    assert "trap cleanup EXIT" in script
    assert "down --volumes --remove-orphans" in script
    assert "openssl rand" in script
    assert "infra/dc" not in script  # never the live project
    for line in script.splitlines():  # every docker compose call inside ssh gets </dev/null
        if line.lstrip().startswith("rt ") or " rt " in line:
            assert "</dev/null" in line or line.rstrip().endswith("\\"), line


def test_the_restore_test_runs_monthly() -> None:
    timer = (INFRA / "systemd" / "optlab-restore-test.timer").read_text(encoding="utf-8")
    service = (INFRA / "systemd" / "optlab-restore-test.service").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-01 03:00:00 UTC" in timer
    assert "Persistent=true" in timer
    assert "User=deploy" in service
    assert "ExecStart=/srv/btc-options-lab/infra/restore-test" in service


def test_docker_builds_never_see_secrets_or_data() -> None:
    ignored = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for pattern in (".env", ".env.*", "data/", ".git/", "backups/"):
        assert pattern in ignored
    dockerfile = (INFRA / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY . " not in dockerfile
    assert "\nUSER optlab\n" in dockerfile


def test_compose_wrapper_refuses_to_print_secrets() -> None:
    wrapper = (INFRA / "dc").read_text(encoding="utf-8")
    assert '"config"' in wrapper
    assert "--env-file .env" in wrapper
    assert (INFRA / "dc").stat().st_mode & 0o111, "infra/dc must be executable"


def test_db_entrypoint_parses_database_url() -> None:
    script = (INFRA / "db" / "entrypoint.sh").read_text(encoding="utf-8")
    assert "unset DATABASE_URL" in script
    assert 'exec docker-entrypoint.sh "$@"' in script


def test_the_rough_paper_service_runs_alone_with_a_memory_limit_and_no_port() -> None:
    paper = compose()["services"]["paper"]
    assert paper["command"] == ["optlab-paper-rough"]
    assert paper["mem_limit"] == "512m"
    assert paper["restart"] == "unless-stopped"
    assert "ports" not in paper
    assert set(paper["environment"]) == {
        "DATABASE_URL",
        "LIVE_TRADING",
        "TRADING_MODE",
        "DERIBIT_ENV",
    }


def test_unattended_upgrades_reboot_at_0400_utc_only_when_needed() -> None:
    """Restart automatically at 04:00 (the server runs on UTC), only when an update needs it."""
    text = (INFRA / "apt" / "52optlab-reboot").read_text(encoding="utf-8")
    lines = {line.strip() for line in text.splitlines() if not line.lstrip().startswith("//")}
    assert 'Unattended-Upgrade::Automatic-Reboot "true";' in lines
    assert 'Unattended-Upgrade::Automatic-Reboot-WithUsers "true";' in lines
    assert 'Unattended-Upgrade::Automatic-Reboot-Time "04:00";' in lines
    # Sorts after Ubuntu's 50unattended-upgrades, so these values win.
    assert "52optlab-reboot" > "50unattended-upgrades"


def test_no_container_gets_the_docker_socket() -> None:
    """Only the host (systemd, as deploy) starts containers; none can control Docker."""
    for name in ("compose.yaml", "restore-test.compose.yaml"):
        for service, spec in compose(name)["services"].items():
            for volume in spec.get("volumes", []):
                assert "docker.sock" not in str(volume), f"{name} {service}"
            assert not spec.get("privileged"), f"{name} {service}"
