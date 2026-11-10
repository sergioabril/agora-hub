from pathlib import Path
from shutil import copy

import pytest
import yaml


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    examples = Path(__file__).parents[1] / "examples"
    for name in ("machines.yaml", "deployments.yaml"):
        copy(examples / name, tmp_path / name)
    hub = yaml.safe_load((examples / "hub.yaml").read_text())
    hub["api"]["admin_token"] = "a" * 64
    hub["home_assistant"]["token"] = "test-ha-token"
    (tmp_path / "hub.yaml").write_text(yaml.safe_dump(hub))
    (tmp_path / "known_hosts").write_text("")
    return tmp_path
