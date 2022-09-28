# Copyright (C) 2022 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, OrderedDict, TextIO, Union

from pydantic import BaseModel, ValidationError, validator
from ruamel.yaml import YAML

from .exceptions import CondaProjectError

PROJECT_YAML_FILENAMES = ("conda-project.yml", "conda-project.yaml")
ENVIRONMENT_YAML_FILENAMES = ("environment.yml", "environment.yaml")

yaml = YAML(typ="rt")
yaml.default_flow_style = False
yaml.block_seq_indent = 2
yaml.indent = 2


class BaseYaml(BaseModel):
    def yaml(self, stream: Union[TextIO, Path]):
        # Passing through self.json() allows json_encoders
        # to serialize objects.
        encoded = json.loads(self.json())
        return yaml.dump(encoded, stream)

    @classmethod
    def parse_yaml(cls, fn: Union[str, Path]):
        d = yaml.load(fn)
        if d is None:
            msg = (
                f"Failed to read {fn} as {cls.__name__}. The file appears to be empty."
            )
            raise CondaProjectError(msg)
        try:
            return cls(**d)
        except ValidationError as e:
            msg = f"Failed to read {fn} as {cls.__name__}\n{str(e)}"
            raise CondaProjectError(msg)

    class Config:
        json_encoders = {Path: lambda v: v.as_posix()}
        extra = "forbid"


class Command(BaseYaml):
    cmd: str
    environment: Optional[str] = None


class CondaProjectYaml(BaseYaml):
    name: str
    environments: OrderedDict[str, List[Path]]
    variables: Dict[str, Optional[str]] = {}
    commands: OrderedDict[str, Union[Command, str]] = OrderedDict()


class EnvironmentYaml(BaseYaml):
    name: Optional[str] = None
    channels: Optional[List[str]] = None
    dependencies: List[Union[str, Dict[str, List[str]]]] = []
    variables: Optional[Dict[str, str]] = {}
    prefix: Optional[Path] = None
    platforms: Optional[List[str]] = None

    @validator("dependencies")
    def only_pip_key_allowed(cls, v):
        for item in v:
            if isinstance(item, dict):
                if not item.keys() == {"pip"}:
                    raise ValueError(
                        f'The dependencies key contains an invalid map {item}. Only "pip:" is allowed.'
                    )
        return v
