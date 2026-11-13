"""Static checks of the deployment files: restarts, no published ports, no secrets in images."""

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
INFRA = REPO_ROOT / "infra"


def compose() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((INFRA / "compose.yaml").read_text(encoding="utf-8"))
    return data


def test_services_and_restart_policy() -> None:
    services = compose()["services"]
    assert set(services) == {"db", "recorder", "health"}
    for name, service in services.items():
        assert service["restart"] == "unless-stopped", name


def test_no_service_publishes_a_port_or_uses_the_host_network() -> None:
    for name, service in compose()["services"].items():
        assert "ports" not in service, name
        assert "expose" not in service, name
        assert service.get("network_mode") != "host", name


def test_apps_get_only_the_settings_they_need() -> None:
    services = compose()["services"]
    recorder, health = services["recorder"]["environment"], services["health"]["environment"]
    for env in (recorder, health):
        assert env["LIVE_TRADING"] == "${LIVE_TRADING:-false}"
        assert env["DATABASE_URL"].startswith("${DATABASE_URL:?")
    assert not any(k.startswith(("HC_", "R2_", "DERIBIT_TESTNET", "CLOUDFLARE")) for k in recorder)
    assert set(k for k in health if k.startswith("HC_")) == {
        "HC_PING_URL_RECORDER",
        "HC_PING_URL_DISK",
    }
    for service in services.values():
        assert "env_file" not in service  # never hand a container the whole .env


def test_images_are_pinned_and_apps_wait_for_the_database() -> None:
    services = compose()["services"]
    assert services["db"]["image"].startswith("timescale/timescaledb:2.")
    assert not services["db"]["image"].endswith(":latest")
    for name in ("recorder", "health"):
        assert services[name]["depends_on"]["db"]["condition"] == "service_healthy"


def test_docker_builds_never_see_secrets_or_data() -> None:
    ignored = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    for pattern in (".env", ".env.*", "data/", ".git/"):
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
