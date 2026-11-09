from pathlib import Path
from shutil import copy

import pytest


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.delenv("AGORA_HUB_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("HOME_ASSISTANT_TOKEN", raising=False)
    examples = Path(__file__).parents[1] / "examples"
    for name in ("hub.yaml", "machines.yaml", "deployments.yaml"):
        copy(examples / name, tmp_path / name)
    (tmp_path / "known_hosts").write_text("")
    (tmp_path / ".env").write_text(
        "AGORA_HUB_ADMIN_TOKEN=" + "a" * 64 + "\nHOME_ASSISTANT_TOKEN=test-ha-token\n"
    )
    return tmp_path
