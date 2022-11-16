# -*- coding: utf-8 -*-
# Copyright (C) 2022 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
from __future__ import annotations

import logging
import os
import sys
from collections import OrderedDict
from pathlib import Path
from typing import List, Optional, Union

from pydantic import create_model

from .conda import current_platform
from .exceptions import CondaProjectError
from .models import BaseModel, Command, Environment
from .project_file import (
    ENVIRONMENT_YAML_FILENAMES,
    PROJECT_YAML_FILENAMES,
    CondaProjectYaml,
    EnvironmentYaml,
    yaml,
)
from .utils import find_file

DEFAULT_PLATFORMS = set(["osx-64", "win-64", "linux-64", current_platform()])

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("CONDA_PROJECT_LOGLEVEL", "WARNING"))


class BaseEnvironments(BaseModel):
    def __getitem__(self, key: str) -> Environment:
        return getattr(self, key)

    def keys(self):
        return self.__dict__.keys()

    def values(self):
        return self.__dict__.values()


class BaseCommands(BaseModel):
    def __getitem__(self, key: str) -> Command:
        return getattr(self, key)

    def keys(self):
        return self.__dict__.keys()

    def values(self):
        return self.__dict__.values()


class CondaProject:
    """A project managed by `conda-project`.

    Attributes:
        directory: The project base directory. Defaults to the current working directory.
        condarc: A path to the local `.condarc` file. Defaults to `<directory>/.condarc`.
        environment_file: A path to the environment file.
        lock_file: A path to the conda-lock file.

    Args:
        directory: The project base directory.

    Raises:
        CondaProjectError: If no suitable environment file is found.

    """

    def __init__(self, directory: Union[Path, str] = "."):
        self.directory = Path(directory).resolve()
        logger.info(f"created Project instance at {self.directory}")

        self.project_yaml_path = find_file(self.directory, PROJECT_YAML_FILENAMES)
        if self.project_yaml_path is not None:
            self._project_file = CondaProjectYaml.parse_yaml(self.project_yaml_path)
        else:
            options = " or ".join(PROJECT_YAML_FILENAMES)
            logger.info(
                f"No {options} file was found. Checking for environment YAML files."
            )

            environment_yaml_path = find_file(
                self.directory, ENVIRONMENT_YAML_FILENAMES
            )
            if environment_yaml_path is None:
                options = " or ".join(ENVIRONMENT_YAML_FILENAMES)
                raise CondaProjectError(f"No Conda {options} file was found.")

            self._project_file = CondaProjectYaml(
                name=self.directory.name,
                environments=OrderedDict(
                    [("default", [environment_yaml_path.relative_to(self.directory)])]
                ),
            )

        self.condarc = self.directory / ".condarc"

    @classmethod
    def create(
        cls,
        directory: Union[Path, str] = ".",
        name: Optional[str] = None,
        dependencies: Optional[List[str]] = None,
        channels: Optional[List[str]] = None,
        platforms: Optional[List[str]] = None,
        conda_configs: Optional[List[str]] = None,
        lock_dependencies: bool = True,
        verbose: bool = False,
    ) -> CondaProject:
        """Create a new project

        Creates the environment.yml file from the specified dependencies,
        channels, and platforms. Further a local .condarc can also be
        created in the directory.

        Args:
            directory:         The path to use as the project directory. The directory
                               will be created if it doesn't exist.
            name:              Name of the project. The default is the basename of the project
                               directory.
            dependencies:      List of package dependencies to include in the environment.yml in
                               MatchSpec format.
            channels:          List of channels to search for dependencies. The default value is
                               ['defaults']
            platforms:         List of platforms over which to lock the dependencies. The default is
                               osx-64, linux-64, win-64 and your current platform if it is not already
                               included.
            conda_configs:     List of Conda configuration parameters to include in the .condarc file
                               written to the project directory.
            lock_dependencies: Create the conda-lock.yml file for the requested dependencies.
                               Default is True.
            force:             Force creation of project and environment files if they already
                               exist. The default value is False.
            verbose:           Print information to stdout. The default value is False.

        Returns:
            CondaProject instance for the project directory.

        """

        directory = Path(directory).resolve()
        if not directory.exists():
            directory.mkdir(parents=True)

        existing_project_file = find_file(directory, PROJECT_YAML_FILENAMES)
        if existing_project_file is not None:
            if verbose:
                print(f"Existing project file found at {existing_project_file}.")
            return cls(directory)

        if name is None:
            name = directory.name

        environment_yaml = EnvironmentYaml(
            channels=channels or ["defaults"],
            dependencies=dependencies or [],
            platforms=platforms or list(DEFAULT_PLATFORMS),
        )

        environment_yaml_path = directory / "environment.yml"
        environment_yaml.yaml(directory / "environment.yml", drop_empty_keys=True)

        project_yaml = CondaProjectYaml(
            name=name,
            environments=OrderedDict(
                [("default", [environment_yaml_path.relative_to(directory)])]
            ),
        )

        project_yaml.yaml(directory / "conda-project.yml")

        condarc = {}
        for config in conda_configs or []:
            k, v = config.split("=")
            condarc[k] = v
        yaml.dump(condarc, directory / ".condarc")

        project = cls(directory)

        if lock_dependencies:
            project.default_environment.lock(verbose=verbose)

        if verbose:
            print(f"Project created at {directory}")

        return project

    @property
    def environments(self) -> BaseEnvironments:
        envs = OrderedDict()
        for env_name, sources in self._project_file.environments.items():
            envs[env_name] = Environment(
                name=env_name,
                sources=tuple(self.directory / s for s in sources),
                prefix=self.directory / "envs" / env_name,
                lockfile=self.directory / f"{env_name}.conda-lock.yml",
                condarc=self.condarc,
            )
        Environments = create_model(
            "Environments",
            **{k: (Environment, ...) for k in envs},
            __base__=BaseEnvironments,
        )
        return Environments(**envs)

    @property
    def default_environment(self) -> Environment:
        name = next(iter(self._project_file.environments))
        return self.environments[name]

    @property
    def commands(self) -> BaseCommands:
        cmds = OrderedDict()
        for name, cmd in self._project_file.commands.items():
            if isinstance(cmd, str):
                cmd_args = cmd
                environment = self.default_environment
            else:
                cmd_args = cmd.cmd
                environment = (
                    self.environments[cmd.environment]
                    if cmd.environment is not None
                    else self.default_environment
                )

            cmds[name] = Command(
                name=name,
                cmd=cmd_args,
                environment=environment,
                variables=self._project_file.variables,
                directory=self.directory,
            )
        Commands = create_model(
            "Commands", **{k: (Command, ...) for k in cmds}, __base__=BaseCommands
        )
        return Commands(**cmds)

    @property
    def default_command(self) -> Command:
        name = next(iter(self._project_file.commands))
        return self.commands[name]

    def check(self, verbose=False) -> bool:
        """Check the project for inconsistencies or errors.

        This will check that .conda-lock.yml files exist for each environment
        and that they are up-to-date against the environment specification.

        Returns:
            Boolean: True if all environments are locked and update to date,
                     False if any environment is not locked or out-of-date.

        """
        return_status = []

        for env in self.environments.values():
            if not env.lockfile.exists():
                if verbose:
                    print(f"The environment {env.name} is not locked.", file=sys.stderr)
                    print(
                        f"Run 'conda project lock {env.name}' to create.",
                        file=sys.stderr,
                    )
                return_status.append(False)
            elif not env.is_locked:
                if verbose:
                    print(
                        f"The lockfile for environment {env.name} is out-of-date.",
                        file=sys.stderr,
                    )
                    print(
                        f"Run 'conda project lock {env.name}' to fix.", file=sys.stderr
                    )
                return_status.append(False)
            else:
                return_status.append(True)

        return all(return_status)
