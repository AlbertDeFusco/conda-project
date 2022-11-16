# -*- coding: utf-8 -*-
# Copyright (C) 2022 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from logging import Logger
from pathlib import Path
from typing import List, Optional

import pexpect
import shellingham
from conda_lock.vendor.conda.utils import wrap_subprocess_call

from .exceptions import CondaProjectError

CONDA_EXE = os.environ.get("CONDA_EXE", "conda")
CONDA_ROOT = os.environ.get("CONDA_ROOT")
CONDA_PREFIX = os.environ.get("CONDA_PREFIX")


def call_conda(
    args: List[str],
    condarc_path: Optional[Path] = None,
    verbose: bool = False,
    logger: Optional[Logger] = None,
    variables: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """Call conda CLI with subprocess.run"""

    parent_process_env = os.environ.copy()

    variables = {} if variables is None else variables
    env = {**variables, **parent_process_env}

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


def conda_run(cmd, prefix, working_dir, variables=None):
    script_path, command = wrap_subprocess_call(
        root_prefix=CONDA_ROOT,
        prefix=prefix,
        dev_mode=False,
        debug_wrapper_scripts=False,
        arguments=shlex.split(cmd),
        use_system_tmp_path=True,
    )

    shell = command[0]

    variables = {} if variables is None else variables
    parent_vars = os.environ.copy()
    env = {**variables, **parent_vars}

    os.chdir(working_dir)
    os.execvpe(shell, ["-c", script_path], env)


def conda_activate(prefix, working_dir, variables=None):
    variables = {} if variables is None else variables
    parent_vars = os.environ.copy()
    env = {**variables, **parent_vars}

    _, shell = shellingham.detect_shell()

    if sys.platform.startswith("win"):
        activate_bat = str(Path(CONDA_ROOT) / "Scripts" / "activate.bat")
        args = ["/K", activate_bat, CONDA_PREFIX]
    else:
        args = ["-il"]

    c = pexpect.spawn(shell, args, cwd=working_dir, env=env)
    c.sendline(f"conda activate {prefix}")
    c.interact()
    c.close()
