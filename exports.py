# Copyright (c) 2017 Linaro Limited.
# Copyright (c) 2017 Open Source Foundries Limited.
#
# SPDX-License-Identifier: Apache-2.0

import os
import shlex
import subprocess

from core import find_sdk_build_root


class ZephyrExports(object):
    """Represents exported variables from a Zephyr build.
    """

    EXPORT = 'Makefile.export'
    HELPER = 'print-value.mk'

    def __init__(self, output_dir):
        self.output_dir = output_dir

    def get(self, variable):
        """Return the value of a Zephyr build variable."""
        export_path = os.path.join(self.output_dir, ZephyrExports.EXPORT)
        helper_path = os.path.join(find_sdk_build_root(), ZephyrExports.HELPER)
        cmd_print_val = ['make',
                         '-f', shlex.quote(export_path),
                         '-f', shlex.quote(helper_path),
                         shlex.quote('print-{}'.format(variable))]
        try:
            value = subprocess.check_output(cmd_print_val,
                                            stderr=subprocess.DEVNULL)
            return str(value, 'utf-8').strip()
        except subprocess.CalledProcessError as e:
            msg = "{} is missing or empty in {}".format(variable, export_path)
            raise ValueError(msg)

    def get_ensure_int(self, variable):
        """Return the value of a Zephyr build variable as a string,
        after ensuring it is an integer."""
        return str(int(self.get(variable), base=0))

    def get_ensure_hex(self, variable):
        """Return the value of a Zephyr build variable as a hex string,
        after ensuring it is an integer."""
        return hex(int(self.get(variable), base=0))
