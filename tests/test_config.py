from pathlib import Path

import pytest
import yaml

from agora_hub.config import ConfigError, load_config


def test_valid_config_loads(config_dir: Path):
    config = load_config(config_dir)

    assert config.hub.api.port == 3333
    assert config.machines["spark-2"].port == 22
    assert config.machines["spark-2"].key_path is None
    assert config.machines["spark-2"].device == "dgx-spark"
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
        ("machines.yaml", lambda data: data["machines"]["spark-1"].update(device="toaster")),
        ("hub.yaml", lambda data: data["api"].update(unknown=True)),
        ("hub.yaml", lambda data: data["api"].update(admin_token="bad")),
        ("hub.yaml", lambda data: data["home_assistant"].update(host="http://homeassistant.local")),
    ],
)
def test_invalid_config_fails(config_dir: Path, file_name, change):
    path = config_dir / file_name
    data = yaml.safe_load(path.read_text())
    change(data)
    path.write_text(yaml.safe_dump(data))

    with pytest.raises(ConfigError):
        load_config(config_dir)


def test_missing_known_hosts_fails(config_dir: Path):
    (config_dir / "known_hosts").unlink()

    with pytest.raises(ConfigError):
        load_config(config_dir)


def test_duplicate_machine_id_fails(config_dir: Path):
    path = config_dir / "machines.yaml"
    path.write_text(path.read_text() + "  spark-1:\n    name: Again\n")

    with pytest.raises(ConfigError, match="spark-1"):
        load_config(config_dir)
