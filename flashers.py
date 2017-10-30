# Copyright (c) 2017 Linaro Limited.
# Copyright (c) 2017 Open Source Foundries Limited.
#
# SPDX-License-Identifier: Apache-2.0

import abc
import os
import shlex
import subprocess

from core import find_app_outdir
from exports import ZephyrExports


class ZephyrBinaryFlasher(abc.ABC):

    def __init__(self, board, app, outdir, debug=False):
        self.board = board
        self.app = app
        self.outdir = outdir
        self.debug_print = debug

    @staticmethod
    def create_flasher(board, app, outdir, debug=False):
        '''Get a flasher instance suited to the given configuration.'''
        app_outdir = find_app_outdir(outdir, app, board, 'app')
        exports = ZephyrExports(app_outdir)
        flash_script = exports.get('FLASH_SCRIPT')

        for sub_cls in ZephyrBinaryFlasher.__subclasses__():
            if sub_cls.is_equivalent_to(flash_script):
                return sub_cls(board, app, outdir, debug=debug)
        msg = 'no supported flasher equivalent to {}'.format(flash_script)
        raise ValueError(msg)

    @staticmethod
    @abc.abstractmethod
    def is_equivalent_to(zephyr_flash_script):
        '''Check if this flasher is also able to flash the same types of
        boards as a FLASH_SCRIPT in the Zephyr build system.'''

    @abc.abstractmethod
    def get_flash_commands(self, device_id, exports, parent, mcuboot_quoted,
                           app_quoted, app_offset, extra_quoted):
        '''Get dictionary of commands needed to perform a flash.

        Keys should be 'mcuboot' and 'app'.

        Values should be iterables of commands. Each command is a
        list, suitable for passing to subprocess.check_call().'''

    def _get_flash_common(self, extra_args, relative=False):
        app_outdir = find_app_outdir(self.outdir, self.app, self.board, 'app')
        # It's fine to use the app's exports to flash mcuboot as well.
        exports = ZephyrExports(app_outdir)
        app_offset = hex(int(exports.get('FLASH_AREA_IMAGE_0_OFFSET'), base=0))
        app_bin = '{}-{}-signed.bin'.format(os.path.basename(self.app),
                                            self.board)
        mcuboot_outdir = find_app_outdir(self.outdir, self.app, self.board,
                                         'mcuboot')
        mcuboot_bin = 'zephyr.bin'

        parent = os.path.commonprefix([app_outdir, mcuboot_outdir])

        if relative:
            app_outdir = app_outdir[len(parent):]
            mcuboot_outdir = mcuboot_outdir[len(parent):]

        app_quoted = shlex.quote(os.path.join(app_outdir, app_bin))
        mcuboot_quoted = shlex.quote(os.path.join(mcuboot_outdir, mcuboot_bin))

        extra_quoted = [shlex.quote(e) for e in extra_args]

        return (exports, parent, mcuboot_quoted, app_quoted, app_offset,
                extra_quoted)

    def flash(self, device_id, extra_args):
        '''Flash the board, taking a list of extra arguments to pass on to
        the underlying flashing tool.'''
        common = self._get_flash_common(extra_args)
        cmds = self.get_flash_commands(device_id, *common)
        mcuboot_cmds = cmds['mcuboot']
        app_cmds = cmds['app']

        if self.debug_print:
            print('Flashing mcuboot:')
            for cmd in mcuboot_cmds:
                print('\t{}'.format(' '.join(cmd)))
        for cmd in mcuboot_cmds:
            subprocess.check_call(cmd)

        if self.debug_print:
            print('Flashing signed application:')
            for cmd in app_cmds:
                print('\t{}'.format(' '.join(cmd)))
        for cmd in app_cmds:
            subprocess.check_call(cmd)

    def quote_sh_list(self, cmd):
        '''Transform a command from list into shell string form.'''
        fmt = ' '.join('{}' for _ in cmd)
        args = [shlex.quote(s) for s in cmd]
        return fmt.format(*args)

    def generate_script(self, fmt):
        '''Generate a script in the given format to flash the board.

        Currently, only shell script is supported.'''
        if fmt != 'sh':
            raise NotImplementedError('fmt must be sh')
        common = self._get_flash_common([], relative=True)
        cmds = self.get_flash_commands(None, *common)
        parent = common[1]
        path = os.path.join(parent, 'flash.sh')

        # Generate the script.
        with open(path, 'w') as f:
            mcuboot_cmds = cmds['mcuboot']
            app_cmds = cmds['app']

            print('#!/bin/sh', file=f)
            print(file=f)
            print('cd $(dirname $(readlink -f $0))', file=f)
            print('# Flash mcuboot:', file=f)
            for cmd in mcuboot_cmds:
                print(self.quote_sh_list(cmd), file=f)
            print('# Flash signed application:', file=f)
            for cmd in app_cmds:
                print(self.quote_sh_list(cmd), file=f)

        # Make the script executable for user and group.
        os.chmod(path, os.stat(path).st_mode | 0o110)


class DfuUtilBinaryFlasher(ZephyrBinaryFlasher):

    def get_flash_commands(self, device_id, exports, parent, mcuboot_quoted,
                           app_quoted, app_offset, extra_quoted):
        # TODO: support non-DfuSe devices. As-is, we support STM32 extensions
        # to the DFU protocol only.
        #
        # We ignore DFUUTIL_DFUSE_ADDR since we're doing a dual-image flash.
        flash_base = exports.get_ensure_hex('CONFIG_FLASH_BASE_ADDRESS')
        app_offset = exports.get_ensure_int('FLASH_AREA_IMAGE_0_OFFSET')
        app_base = hex(int(flash_base, base=16) + int(app_offset))
        pid = exports.get('DFUUTIL_PID')
        pid_arg_quoted = '[{}]'.format(shlex.quote(pid))
        serial = []
        if device_id:
            serial += ['-S', device_id]

        cmd_flash_mcuboot = (['dfu-util',
                              '-d', pid_arg_quoted,
                              '-a', '0',
                              '-s', '{}:force:mass-erase'.format(flash_base),
                              '-D', mcuboot_quoted] +
                             serial)

        cmd_flash_app = (['dfu-util',
                          '-d', pid_arg_quoted,
                          '-a', '0',
                          '-s', '{}:leave'.format(app_base),
                          '-D', app_quoted] +
                         serial)

        return {'mcuboot': [cmd_flash_mcuboot], 'app': [cmd_flash_app]}

    def is_equivalent_to(script):
        return script == 'dfuutil.sh'


class PyOcdBinaryFlasher(ZephyrBinaryFlasher):

    # Invoking pyocd-flashtool again quickly results in errors on some systems.
    SLEEP_INTERVAL_SEC = '1'

    def get_flash_commands(self, device_id, exports, parent, mcuboot_quoted,
                           app_quoted, app_offset, extra_quoted):
        target_quoted = shlex.quote(exports.get('PYOCD_TARGET'))
        board_id = []
        if device_id:
            board_id += ['-b', device_id]

        cmd_flash_mcuboot = (['pyocd-flashtool',
                              '-t', target_quoted,
                              '-ce',
                              '-a', '0x0'] +
                             board_id +
                             extra_quoted +
                             [mcuboot_quoted])
        cmd_sleep_mcuboot = ['sleep', PyOcdBinaryFlasher.SLEEP_INTERVAL_SEC]

        cmd_flash_app = (['pyocd-flashtool',
                          '-t', target_quoted,
                          '-a', app_offset] +
                         board_id +
                         extra_quoted +
                         [app_quoted])

        return {'mcuboot': [cmd_flash_mcuboot, cmd_sleep_mcuboot],
                'app': [cmd_flash_app]}

    def is_equivalent_to(script):
        return script == 'pyocd.sh'
