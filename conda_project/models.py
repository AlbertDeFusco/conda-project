# -*- coding: utf-8 -*-
# Copyright (C) 2022 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import warnings
from contextlib import nullcontext, redirect_stderr
from io import StringIO
from pathlib import Path
from subprocess import SubprocessError
from typing import Tuple

from conda_lock.conda_lock import (
    default_virtual_package_repodata,
    make_lock_files,
    make_lock_spec,
    parse_conda_lock_file,
    render_lockfile_for_platform,
)
from pydantic import BaseModel as PydanticBaseModel

from .conda import CONDA_EXE, call_conda, current_platform
from .exceptions import CondaProjectError
from .project_file import EnvironmentYaml
from .utils import Spinner, env_variable

_TEMPFILE_DELETE = False if sys.platform.startswith("win") else True

DEFAULT_PLATFORMS = set(["osx-64", "win-64", "linux-64", current_platform()])

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.environ.get("CONDA_PROJECT_LOGLEVEL", "WARNING"))


class BaseModel(PydanticBaseModel):
    class Config:
        allow_mutation = False
        extra = "forbid"


class Environment(BaseModel):
    name: str
    sources: Tuple[Path, ...]
    prefix: Path
    lockfile: Path
    condarc: Path

    @property
    def _overrides(self):
        specified_channels = []
        specified_platforms = set()
        for fn in self.sources:
            env = EnvironmentYaml.parse_yaml(fn)
            for channel in env.channels or []:
                if channel not in specified_channels:
                    specified_channels.append(channel)
            if env.platforms is not None:
                specified_platforms.update(env.platforms)

        channel_overrides = None
        if not specified_channels:
            env_files = ",".join([source.name for source in self.sources])
            msg = f"there are no 'channels:' key in {env_files} assuming 'defaults'."
            warnings.warn(msg)
            channel_overrides = ["defaults"]

        platform_overrides = None
        if not specified_platforms:
            platform_overrides = list(DEFAULT_PLATFORMS)

        return channel_overrides, platform_overrides

    @property
    def is_locked(self) -> bool:
        """
        bool: Returns True if the lockfile is consistent with the source files, False otherwise.
        """
        channel_overrides, platform_overrides = self._overrides
        if self.lockfile.exists():
            lock = parse_conda_lock_file(self.lockfile)
            spec = make_lock_spec(
                src_files=list(self.sources),
                channel_overrides=channel_overrides,
                platform_overrides=platform_overrides,
                virtual_package_repo=default_virtual_package_repodata(),
            )
            all_up_to_date = all(
                p in lock.metadata.platforms
                and spec.content_hash_for_platform(p) == lock.metadata.content_hash[p]
                for p in spec.platforms
            )
            return all_up_to_date
        else:
            return False

    @property
    def is_prepared(self) -> bool:
        """
        bool: Returns True if the Conda environment exists and is consistent with
              the environment source and lock files, False otherwise. If is_locked is
              False is_prepared is False.
        """
        if (self.prefix / "conda-meta" / "history").exists():
            if self.is_locked:
                installed_pkgs = call_conda(
                    ["list", "-p", str(self.prefix), "--explicit"]
                ).stdout.splitlines()[3:]

                lock = parse_conda_lock_file(self.lockfile)
                rendered = render_lockfile_for_platform(
                    lockfile=lock,
                    platform=current_platform(),
                    kind="explicit",
                    include_dev_dependencies=False,
                    extras=None,
                )
                locked_pkgs = [p.split("#")[0] for p in rendered[3:]]

                return installed_pkgs == locked_pkgs

        return False

    def lock(
        self,
        force: bool = False,
        verbose: bool = False,
    ) -> None:
        """Generate locked package lists for the supplied or default platforms

        Utilizes conda-lock to build the .conda-lock.yml file.

        Args:
            force:       Rebuild the .conda-lock.yml file even if no changes were made
                         to the dependencies.
            verbose:     A verbose flag passed into the `conda lock` command.

        """
        if self.is_locked and not force:
            if verbose:
                print(
                    f"The lockfile {self.lockfile.name} already exists and is up-to-date.\n"
                    f"Run 'conda project lock --force {self.name} to recreate it from source specification."
                )
            return

        # Setup temporary file for conda-lock to write to.
        # If a package is removed from the environment source
        # after the lockfile has been created conda-lock updates
        # the hash in the lockfile but does not remove the unspecified
        # package (and necessary orphaned dependencies) from the lockfile.
        # To avoid this scenario lockfiles are written to a temporary location
        # and copied back to the self.lockfile path if successful.
        tempdir = Path(tempfile.mkdtemp())
        lockfile = tempdir / self.lockfile.name

        channel_overrides, platform_overrides = self._overrides

        specified_channels = []
        for fn in self.sources:
            env = EnvironmentYaml.parse_yaml(fn)
            for channel in env.channels or []:
                if channel not in specified_channels:
                    specified_channels.append(channel)

        with redirect_stderr(StringIO()) as _:
            with env_variable("CONDARC", str(self.condarc)):
                if verbose:
                    context = Spinner(prefix=f"Locking dependencies for {self.name}")
                else:
                    context = nullcontext()

                with context:
                    try:
                        make_lock_files(
                            conda=CONDA_EXE,
                            src_files=list(self.sources),
                            lockfile_path=lockfile,
                            kinds=["lock"],
                            platform_overrides=platform_overrides,
                            channel_overrides=channel_overrides,
                        )
                        shutil.copy(lockfile, self.lockfile)
                    except SubprocessError as e:
                        output = json.loads(e.output)
                        msg = output["message"].replace(
                            "target environment",
                            f"supplied channels: {channel_overrides or specified_channels}",
                        )
                        msg = "Project failed to lock\n" + msg
                        raise CondaProjectError(msg)
                    finally:
                        shutil.rmtree(tempdir)

        lock = parse_conda_lock_file(self.lockfile)
        msg = f"Locked dependencies for {', '.join(lock.metadata.platforms)} platforms"
        logger.info(msg)

    def prepare(
        self,
        force: bool = False,
        verbose: bool = False,
    ) -> Path:
        """Prepare the conda environment.

        Creates a new conda environment and installs the packages from the environment.yaml file.
        Environments are always created from the conda-lock.yml file. The conda-lock.yml
        will be created if it does not already exist.

        Args:
            force: If True, will force creation of a new conda environment.
            verbose: A verbose flag passed into the `conda create` command.

        Raises:
            CondaProjectError: If no suitable environment file can be found.

        Returns:
            The path to the created environment.

        """
        if not self.is_locked:
            if verbose and self.lockfile.exists():
                print(f"The lockfile {self.lockfile} is out-of-date, re-locking...")
            self.lock(verbose=verbose)

        if self.is_prepared:
            if not force:
                logger.info(f"environment already exists at {self.prefix}")
                if verbose:
                    print(
                        f"The environment already exists and is up-to-date.\n"
                        f"run 'conda project prepare --force {self.name} to recreate it from the locked dependencies."
                    )
                return self.prefix
        elif (self.prefix / "conda-meta" / "history").exists() and not self.is_prepared:
            if not force:
                if verbose:
                    print(
                        f"The environment exists but does not match the locked dependencies.\n"
                        f"Run 'conda project prepare --force {self.name}' to recreate the environment from the "
                        f"locked dependencies."
                    )
                return self.prefix

        lock = parse_conda_lock_file(self.lockfile)
        if current_platform() not in lock.metadata.platforms:
            msg = (
                f"Your current platform, {current_platform()}, is not in the supported locked platforms.\n"
                f"You may need to edit your environment source files and run 'conda project lock' again."
            )
            raise CondaProjectError(msg)

        rendered = render_lockfile_for_platform(
            lockfile=lock,
            platform=current_platform(),
            kind="explicit",
            include_dev_dependencies=False,
            extras=None,
        )

        with tempfile.NamedTemporaryFile(mode="w", delete=_TEMPFILE_DELETE) as f:
            f.write("\n".join(rendered))
            f.flush()

            args = [
                "create",
                "-y",
                *("--file", f.name),
                *("-p", str(self.prefix)),
            ]
            if force:
                args.append("--force")

            _ = call_conda(
                args, condarc_path=self.condarc, verbose=verbose, logger=logger
            )

        with (self.prefix / ".gitignore").open("wt") as f:
            f.write("*")

        variables = {}
        for fn in self.sources:
            env = EnvironmentYaml.parse_yaml(fn)
            variables.update(env.variables)

        if variables:
            args = ["env", "config", "vars", "set"]
            args.extend([f"{k}={v}" for k, v in variables.items()])
            args.extend(["-p", str(self.prefix)])
            _ = call_conda(
                args, condarc_path=self.condarc, verbose=verbose, logger=logger
            )

        msg = f"environment created at {self.prefix}"
        logger.info(msg)
        if verbose:
            print(msg)

        return self.prefix

    def clean(
        self,
        verbose: bool = False,
    ) -> None:
        """Remove the conda environment."""

        _ = call_conda(
            ["env", "remove", "-p", str(self.prefix)],
            condarc_path=self.condarc,
            verbose=verbose,
            logger=logger,
        )
