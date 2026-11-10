import os
import re
import secrets
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, PositiveFloat, SecretStr
from pydantic import ValidationError, constr, field_validator, model_validator

ConfigId = constr(pattern=r"^[a-z0-9][a-z0-9_-]*$")
HubTime = constr(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
ReadinessPath = constr(pattern=r"^/")
MachineName = constr(strip_whitespace=True, min_length=1)


class ConfigError(Exception):
    pass


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class APIConfig(Model):
    host: str
    port: int = 3333
    admin_token: SecretStr = Field(repr=False)

    @field_validator("admin_token")
    @classmethod
    def hex_token(cls, value: SecretStr) -> SecretStr:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", value.get_secret_value()):
            raise ValueError("must contain 64 hex characters")
        return value


class SSHConfig(Model):
    known_hosts_path: Path


class HomeAssistantConfig(Model):
    base_url: HttpUrl
    token: SecretStr = Field(min_length=1, repr=False)
    turn_off_path: str
    turn_on_path: str
    power_cycle_delay_seconds: float = Field(ge=0)


class ScheduleConfig(Model):
    enabled: bool
    timezone: str
    wake_at: HubTime
    shutdown_at: HubTime
    machines: list[str]
    startup_deployment: str | None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("unknown timezone") from error
        return value

    @model_validator(mode="after")
    def distinct_times(self):
        if self.wake_at == self.shutdown_at:
            raise ValueError("wake_at and shutdown_at must differ")
        return self


class TimeoutsConfig(Model):
    ssh_connect_seconds: PositiveFloat
    status_command_seconds: PositiveFloat
    http_seconds: PositiveFloat
    machine_boot_seconds: PositiveFloat
    machine_shutdown_seconds: PositiveFloat
    deployment_start_seconds: PositiveFloat
    deployment_stop_seconds: PositiveFloat
    readiness_poll_seconds: PositiveFloat


class HubConfig(Model):
    api: APIConfig
    ssh: SSHConfig
    home_assistant: HomeAssistantConfig
    schedule: ScheduleConfig
    timeouts: TimeoutsConfig


class MachineConfig(Model):
    name: MachineName
    host: str
    user: str
    port: int = 22
    password: SecretStr | None = Field(default=None, repr=False)
    key_path: Path | None = None
    sudo_password: SecretStr | None = Field(default=None, repr=False)
    shutdown_command: str = "sudo -S -p '' poweroff"
    plug_entity_id: str
    device: Literal["dgx-spark", "rtx6000", "mac-studio"] = "dgx-spark"

    @model_validator(mode="after")
    def has_authentication(self):
        if self.password is None and self.key_path is None:
            raise ValueError("password or key_path is required")
        return self


class ReadinessConfig(Model):
    path: ReadinessPath
    expected_status: int


class DeploymentConfig(Model):
    name: str
    machines: list[str]
    head: str
    working_directory: str
    start_command: str
    stop_command: str
    status_command: str | None = None
    endpoint: HttpUrl
    readiness: ReadinessConfig


class Config(Model):
    hub: HubConfig
    machines: dict[ConfigId, MachineConfig]
    deployments: dict[ConfigId, DeploymentConfig]
    config_dir: Path

    @model_validator(mode="after")
    def validate_config(self):
        self.hub.ssh.known_hosts_path = self.config_dir / self.hub.ssh.known_hosts_path
        if not self.hub.ssh.known_hosts_path.is_file():
            raise ValueError(f"known_hosts file not found: {self.hub.ssh.known_hosts_path}")
        for machine in self.machines.values():
            machine.key_path = self.config_dir / machine.key_path if machine.key_path else None
        machine_ids = set(self.machines)
        if "add" in machine_ids:
            raise ValueError("machine id add is reserved")
        schedule = self.hub.schedule
        if unknown := set(schedule.machines) - machine_ids:
            raise ValueError(f"unknown schedule machine: {sorted(unknown)[0]}")
        if schedule.startup_deployment not in set(self.deployments) | {None}:
            raise ValueError(f"unknown schedule deployment: {schedule.startup_deployment}")
        for deployment_id, deployment in self.deployments.items():
            if unknown := set(deployment.machines) - machine_ids:
                raise ValueError(f"unknown machine in {deployment_id}: {sorted(unknown)[0]}")
            if deployment.head not in deployment.machines:
                raise ValueError(f"head is not in machines for {deployment_id}")
        if schedule.startup_deployment:
            startup = self.deployments[schedule.startup_deployment]
            if not set(startup.machines) <= set(schedule.machines):
                raise ValueError("startup_deployment machines must be schedule machines")
        return self


class _UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if duplicates := [key for key in keys if keys.count(key) > 1]:
            raise yaml.YAMLError(f"duplicate key {duplicates[0]!r}")
        return super().construct_mapping(node, deep=deep)


def _read_yaml(path: Path) -> dict:
    try:
        value = yaml.load(path.read_text(), Loader=_UniqueKeyLoader)
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"cannot load {path.name}: {error}") from error
    if not isinstance(value, dict):
        raise ConfigError(f"{path.name} must contain a mapping")
    return value


def load_config(config_dir: Path) -> Config:
    config_dir = Path(config_dir).expanduser().resolve()
    hub_yaml = _read_yaml(config_dir / "hub.yaml")
    machines_yaml = _read_yaml(config_dir / "machines.yaml")
    deployments_yaml = _read_yaml(config_dir / "deployments.yaml")
    try:
        return Config(hub=hub_yaml, config_dir=config_dir, **machines_yaml, **deployments_yaml)
    except ValidationError as error:
        raise ConfigError(_error_message(error)) from error


def _error_message(error: ValidationError) -> str:
    item = error.errors()[0]
    location = ".".join(str(part) for part in item["loc"])
    return f"invalid configuration at {location}: {item['msg']}"


def save_machine(config: Config, machine_id: str | None, changes: dict) -> dict:
    path = config.config_dir / "machines.yaml"
    data = _read_yaml(path)
    machines = data["machines"]
    if machine_id is None:
        machine_id = secrets.token_hex(4)
        while machine_id in config.machines or machine_id in machines:
            machine_id = secrets.token_hex(4)
    entry = dict(machines.get(machine_id) or {})
    for key, value in changes.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    try:
        machine = MachineConfig.model_validate(entry)
    except ValidationError as error:
        raise ConfigError(_error_message(error)) from error
    machines[machine_id] = entry
    temporary = path.with_name("machines.yaml.tmp")
    temporary.write_text(yaml.safe_dump(data, sort_keys=False))
    temporary.chmod(0o600)
    os.replace(temporary, path)
    machine.key_path = config.config_dir / machine.key_path if machine.key_path else None
    config.machines[machine_id] = machine
    view = machine.model_dump(mode="json", exclude={"password", "sudo_password"})
    return {"id": machine_id, **view, "key_path": entry.get("key_path")}
