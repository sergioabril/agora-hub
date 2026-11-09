from pathlib import Path

import pytest
import yaml

from agora_hub.config import ConfigError, load_config


def test_valid_config_loads(config_dir: Path):
    config = load_config(config_dir)

    assert config.hub.api.port == 3333
    assert config.machines["spark-2"].port == 22
    assert config.machines["spark-2"].key_path is None
    assert config.deployments["pro"].status_command == "bash status.sh"
    assert config.hub.ssh.known_hosts_path == config_dir / "known_hosts"


@pytest.mark.parametrize(
    ("file_name", "change"),
    [
        ("deployments.yaml", lambda data: data["deployments"]["pro"].update(head="missing")),
        ("deployments.yaml", lambda data: data["deployments"]["pro"].update(head="spark-2", machines=["spark-1"])),
        ("hub.yaml", lambda data: data["schedule"].update(machines=["missing"])),
        ("hub.yaml", lambda data: data["schedule"].update(startup_deployment="missing")),
        ("hub.yaml", lambda data: data["schedule"].update(timezone="Nowhere/Imaginary")),
        ("hub.yaml", lambda data: data["schedule"].update(wake_at="8am")),
        ("hub.yaml", lambda data: data["timeouts"].update(http_seconds=0)),
        ("machines.yaml", lambda data: data["machines"]["spark-1"].update(password=None, key_path=None)),
        ("machines.yaml", lambda data: data["machines"].update({"Bad ID": data["machines"]["spark-1"]})),
        ("hub.yaml", lambda data: data["api"].update(unknown=True)),
    ],
)
def test_invalid_config_fails(config_dir: Path, file_name, change):
    path = config_dir / file_name
    data = yaml.safe_load(path.read_text())
    change(data)
    path.write_text(yaml.safe_dump(data))

    with pytest.raises(ConfigError):
        load_config(config_dir)


def test_bad_admin_token_fails(config_dir: Path):
    (config_dir / ".env").write_text(
        "AGORA_HUB_ADMIN_TOKEN=bad\nHOME_ASSISTANT_TOKEN=test-ha-token\n"
    )

    with pytest.raises(ConfigError):
        load_config(config_dir)


def test_missing_known_hosts_fails(config_dir: Path):
    (config_dir / "known_hosts").unlink()

    with pytest.raises(ConfigError):
        load_config(config_dir)
