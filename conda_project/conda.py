# -*- coding: utf-8 -*-
# Copyright (C) 2022 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
from __future__ import annotations

import collections
import itertools
import json
import os
import subprocess
from logging import Logger
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from conda_lock.vendor.conda.core.prefix_data import (
    PackageRecord,
    PrefixData,
    PrefixGraph,
)
from conda_lock.vendor.conda.models.enums import PackageType

from .exceptions import CondaProjectError

CONDA_EXE = os.environ.get("CONDA_EXE", "conda")


def _groupby(keyfunc, sequence):
    """
    toolz-style groupby, returns a dictionary of { key: [group] } instead of
    iterators.
    """
    result = collections.defaultdict(lambda: [])
    for key, group in itertools.groupby(sequence, keyfunc):
        result[key].extend(group)
    return dict(result)


def call_conda(
    args: List[str],
    condarc_path: Optional[Path] = None,
    verbose: bool = False,
    logger: Optional[Logger] = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if condarc_path is not None:
        if logger is not None:
            logger.info(f"setting CONDARC env variable to {condarc_path}")
        env["CONDARC"] = str(condarc_path)

    cmd = [CONDA_EXE] + args

    if verbose:
        stdout = None
    else:
        stdout = subprocess.PIPE

    if logger is not None:
        logger.info(f'running conda command: {" ".join(cmd)}')
    proc = subprocess.run(
        cmd, env=env, stdout=stdout, stderr=subprocess.PIPE, encoding="utf-8"
    )

    if proc.returncode != 0:
        print_cmd = " ".join(cmd)
        raise CondaProjectError(f"Failed to run:\n  {print_cmd}\n{proc.stderr.strip()}")

    return proc


def conda_info():
    proc = call_conda(["info", "--json"])
    parsed = json.loads(proc.stdout)
    return parsed


def current_platform():
    info = conda_info()
    return info.get("platform")


def get_installed_packages(prefix) -> Dict[str, list]:
    pd = PrefixData(prefix_path=prefix, pip_interop_enabled=True)

    # This section borrowed from conda_env::env.py
    precs: Tuple[PackageRecord] = tuple(PrefixGraph(pd.iter_records()).graph)
    grouped_precs = _groupby(lambda x: x.package_type, precs)
    conda_precs = sorted(
        (
            *grouped_precs.get(None, ()),
            *grouped_precs.get(PackageType.NOARCH_GENERIC, ()),
            *grouped_precs.get(PackageType.NOARCH_PYTHON, ()),
        ),
        key=lambda x: x.name,
    )
    conda_sha256 = sorted([p.sha256 for p in conda_precs])

    pip_precs = sorted(
        (
            *grouped_precs.get(PackageType.VIRTUAL_PYTHON_WHEEL, ()),
            *grouped_precs.get(PackageType.VIRTUAL_PYTHON_EGG_MANAGEABLE, ()),
            *grouped_precs.get(PackageType.VIRTUAL_PYTHON_EGG_UNMANAGEABLE, ()),
            # *grouped_precs.get(PackageType.SHADOW_PYTHON_EGG_LINK, ()),
        ),
        key=lambda x: x.name,
    )
    pip_names = [p.name for p in pip_precs]

    try:
        pip_freeze = call_conda(["run", "-p", prefix, "pip", "freeze"])
        pip_sha256 = sorted(
            [
                p.split()[2].split("=")[1]
                for p in pip_freeze.stdout.strip().splitlines()
                if p.split()[0] in pip_names
            ]
        )
    except CondaProjectError as e:
        if "pip: command not found" in str(e):
            pip_sha256 = []
        else:
            raise e

    installed_packages = {"conda": conda_sha256, "pip": pip_sha256}

    return installed_packages
