#!/usr/bin/env python3

# Copyright (c) 2017 Linaro Limited.
# Copyright (c) 2017 Open Source Foundries Limited.
#
# SPDX-License-Identifier: Apache-2.0

import abc
import argparse
import multiprocessing
import os
import os.path
import platform
import re
import shlex
import subprocess
import sys

#
# Globals
#

PROGRAM = sys.argv[0]
ARGV = sys.argv[1:]

# We could be smarter about this (search for .repo, e.g.), but it seems
# unnecessary.
ZMP_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

# Default values shared by multiple commands.
BOARD_DEFAULT = '96b_nitrogen'
ZEPHYR_GCC_VARIANT_DEFAULT = 'gccarmemb'
CONF_FILE_DEFAULT = 'prj.conf'
BUILD_PARALLEL_DEFAULT = multiprocessing.cpu_count()

# Checked out paths for important repositories relative to ZMP root.
# TODO: parse these from the repo manifest, for robustness, at some point.
ZEPHYR_PATH = 'zephyr'
ZEPHYR_SDK_PATH = os.path.join('sdk-prebuilts', 'zephyr-sdk')

# Build configuration from command line options that overrides environment
# variables. Note that BOARD is special, since we can target multiple boards in
# one script invocation, so we don't include it in this list.
BUILD_OPTIONS = ['conf_file',
                 'zephyr_gcc_variant',
                 # 'board',
                 ]
# What types of build outputs to produce.
# - app: ZMP-ready application, which can be signed for flashing or FOTA.
# - mcuboot: ZMP bootloader, not signed and must be flashed.
BUILD_OUTPUTS = ['app', 'mcuboot']
# The name of the directory which is the default root of the build hierarchy,
# relative to the .repo root.
BUILD_DIR_DEFAULT = 'outdir'

# Where mcuboot is relative to the .repo top level.
MCUBOOT_PATH = 'mcuboot'
# Development-only firmware binary signing key.
MCUBOOT_DEV_KEY = 'root-rsa-2048.pem'
# Version to write to signed binaries when none is specified.
MCUBOOT_IMGTOOL_VERSION_DEFAULT = '0.0.0'
# imgtool.py state. This post-processes binaries for chain-loading by mcuboot.
MCUBOOT_IMGTOOL = os.path.join('scripts', 'imgtool.py')
# mcuboot-related SoC-specific state.
# TODO: get these values from ZephyrExports when they're available there.
MCUBOOT_WORD_SIZES = {
    '96b_nitrogen': '4',
    '96b_carbon': '1',
    'frdm_k64f': '8',
    'nrf52840_pca10056': '4',
    'nrf52_blenano2': '4',
}

# Programs which 'configure' can use to generate Zephyr .config files.
CONFIGURATORS = ['config', 'nconfig', 'menuconfig', 'xconfig', 'gconfig',
                 'oldconfig', 'silentoldconfig', 'defconfig', 'savedefconfig',
                 'allnoconfig', 'allyesconfig', 'alldefconfig', 'randconfig',
                 'listnewconfig', 'olddefconfig']
# menuconfig is portable and the one most examples are based off of.
CONFIGURATOR_DEFAULT = 'menuconfig'

# The documentation source repository, as well as the name of the root of the
# build hierarchy under BUILD_DIR_DEFAULT or where the user places it.
DOC_PATH = 'doc'
# Supported formats for the generated documentation.
DOC_OUTPUT_FORMATS = ['html',  'dirhtml', 'singlehtml']
# We expect most users to read the docs as multiple HTML pages.
DOC_FORMAT_DEFAULT = 'html'


#
# Path management
#


def find_zmp_root():
    '''Get absolute path of root directory of this ZMP installation.'''
    return ZMP_ROOT


def find_zephyr_base():
    '''Get absolute path of ZMP Zephyr base directory.'''
    return os.path.join(find_zmp_root(), ZEPHYR_PATH)


def find_arm_none_eabi_gcc():
    '''Get absolute path of ZMP prebuilt GCC ARM Embedded.'''
    platform_subdirectories = {
        'Linux': 'linux',
        'Darwin': 'mac',
        }
    subdir = platform_subdirectories[platform.system()]
    return os.path.join(find_zmp_root(),
                        'build',
                        'other',
                        'zmp-prebuilt',
                        'arm-none-eabi-gcc',
                        subdir)


def find_app_root(app_name):
    '''Get absolute path of app within ZMP Zephyr SDK.'''
    return os.path.join(find_zmp_root(), app_name)


def find_mcuboot_root():
    '''Get absolute path of mcuboot repository.'''
    return os.path.join(find_zmp_root(), MCUBOOT_PATH)


def find_sdk_build_root():
    '''Get absolute path to SDK build directory.'''
    return os.path.dirname(os.path.realpath(__file__))


def find_doc_root():
    '''Get absolute path of documentation source code repository.'''
    return os.path.join(find_zmp_root(), DOC_PATH)


def find_default_outdir():
    '''Get absolute path of default output directory.'''
    return os.path.join(find_zmp_root(), BUILD_DIR_DEFAULT)


def find_app_outdir(outdir, app, board, output):
    '''Get output (build) directory for an app output.'''
    return os.path.join(outdir, app, board, output)


#
# Zephyr build system glue
#


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


#
# Command base class
#


class Command(abc.ABC):
    '''Parent class for runnable commands.'''

    HELP = {
        # Generally useful.
        '--board': '''Zephyr board to target (default: {}). This may be
                   given multiple times to target additional boards.'''.format(
                       BOARD_DEFAULT),
        '--outdir': '''Build directory (default: '{}' under ZMP
                    root).'''.format(BUILD_DIR_DEFAULT),
        'app': 'application(s) sources',

        # Needed to build, configure, etc. Zephyr.
        '--zephyr-gcc-variant': '''Toolchain variant used by Zephyr
                       (default: {})'''.format(ZEPHYR_GCC_VARIANT_DEFAULT),
        '--prebuilt-toolchain': '''Whether to use a pre-built toolchain
                       provided with ZMP, if one exists (default: 'yes').
                       Currently, only a pre-built GCC ARM Embedded toolchain
                       is provided. Set to 'no' to prevent overriding the
                       toolchain's location in the calling environment.''',
        '--conf-file': '''App (not mcuboot) configuration file
                       (default: {})'''.format(CONF_FILE_DEFAULT),
        '--jobs': '''Number of jobs to run simultaneously (default: number of
                   available CPUs)''',
        '--keep-going': '''If set, keep running after the first build failure.
                         Otherwise, exit on the first failure.''',
        '--outputs': 'Which outputs to target (default: all)',
    }

    def __init__(self, stdout=sys.stdout, stderr=sys.stderr, whitelist=None):
        '''Create a new Command object, with options to whitelist commands.

        If whitelist is None, all commands are whitelisted.  Otherwise,
        it must be an iterable of common arguments to whitelist.'''
        self.stdout = stdout
        self.stderr = stderr

        all = Command.HELP.keys()
        if whitelist is None:
            self.whitelist = all
        else:
            whitelist = set(whitelist)
            if not whitelist.issubset(all):
                bad_args = whitelist.difference(all)
                msg = 'internal error: bad arguments {}'.format(bad_args)
                raise ValueError(msg)
            self.whitelist = whitelist

    #
    # Abstract interfaces and overridable behavior.
    #

    @abc.abstractproperty
    def command_name(self):
        '''The name of this command as invoked by users.'''

    @abc.abstractproperty
    def command_help(self):
        '''The top-level help string for this command to display to users.'''

    def arg_help(self, argument):
        '''Get help text for an argument provided by an abstract Command.

        Subclasses may override this to provide specialized help text.'''
        if argument not in Command.HELP:
            msg = ('internal error: no help available for unknown argument' +
                   '{}'.format(argument))
            raise ValueError(msg)
        return Command.HELP[argument]

    def do_register(self, parser):
        '''Subclasses may override to register a register() callback.'''
        pass

    def do_prep_for_run(self, environment):
        '''Subclasses may override to register a prep_for_run() callback.

        When this method is invoked, self.arguments contains the
        command's arguments.

        The argument `environment' is a mutable dict-like that will be
        used as a starting point for self.make_envs, if that is created
        in the prep_for_run() call. Subclasses can modify it as needed,
        though note that the values it contains may be overridden by the
        Command core.'''
        pass

    @abc.abstractmethod
    def do_invoke(self):
        '''Handle command-specific invocation work.

        When this method is called, self.arguments contains the command
        arguments, and self.prep_for_run() has been called.'''

    #
    # Printing helpers for use here and by subclasses. Rules:
    #
    # 1. Don't be chatty with dbg().
    # 2. No printing errors! Just raise an exception.
    #

    def dbg(self, *args, sep='  ', end='\n', flush=False):
        '''Display a message, only if --debug was given.'''
        if self.arguments.debug:
            print(*args, sep=sep, end=end, file=self.stdout, flush=flush)

    def wrn(self, *args, sep='  ', end='\n', flush=False):
        '''Display a warning message.'''
        print(*args, sep=sep, end=end, file=self.stderr, flush=flush)

    def dbg_make_cmd(self, msg, cmd, env=None, board=None):
        '''Special case helper for debugging invocations of make.'''
        if self.arguments.debug:
            self.dbg('{}:'.format(msg))
            if board is not None:
                self.dbg('\tBOARD={} \\'.format(board))
            if env is not None:
                for arg in self.make_overrides:
                    env_var = self.arg_to_env_var(arg)
                    if env_var not in env:
                        continue
                    self.dbg('\t{}={} \\'.format(env_var, env[env_var]))
            self.dbg('\t' + ' '.join(cmd))

    #
    # Command core
    #

    def register(self, parsers):
        '''Register a command with a parser, adding arguments.

        Any whitelist passed at instantiation time will be used as a
        filter on arguments to add.'''
        parser = parsers.add_parser(self.command_name, help=self.command_help)

        # These are generally useful for commands that operate on build
        # artifacts.
        if '--board' in self.whitelist:
            parser.add_argument('-b', '--board', dest='boards',
                                default=[], action='append',
                                help=self.arg_help('--board'))
        if '--outdir' in self.whitelist:
            parser.add_argument('-O', '--outdir',
                                default=find_default_outdir(),
                                help=self.arg_help('--outdir'))
        if 'app' in self.whitelist:
            parser.add_argument('app', nargs='+', help=self.arg_help('app'))

        # These are needed by commands that invoke 'make', like 'build' and
        # 'configure'.
        if '--zephyr-gcc-variant' in self.whitelist:
            parser.add_argument('-z', '--zephyr-gcc-variant',
                                default=ZEPHYR_GCC_VARIANT_DEFAULT,
                                help=self.arg_help('--zephyr-gcc-variant'))
        if '--prebuilt-toolchain' in self.whitelist:
            parser.add_argument('--prebuilt-toolchain', default='yes',
                                choices=['yes', 'no', 'y', 'n'],
                                help=self.arg_help('--prebuilt-toolchain'))
        if '--conf-file' in self.whitelist:
            parser.add_argument('-c', '--conf-file', default=CONF_FILE_DEFAULT,
                                help=self.arg_help('--conf-file'))
        if '--jobs' in self.whitelist:
            parser.add_argument('-j', '--jobs',
                                type=int, default=BUILD_PARALLEL_DEFAULT,
                                help=self.arg_help('--jobs'))
        if '--keep-going' in self.whitelist:
            parser.add_argument('-k', '--keep-going', action='store_true',
                                help=self.arg_help('--keep-going'))
        if '--outputs' in self.whitelist:
            parser.add_argument('-o', '--outputs',
                                choices=BUILD_OUTPUTS + ['all'], default='all',
                                help=self.arg_help('--outputs'))

        # The following toolchain-related arguments must all be in the
        # whitelist, if any of them are.
        toolchain_wl = ['--outputs',
                        '--zephyr-gcc-variant',
                        '--prebuilt-toolchain']
        toolchain_wl_ok = (all(x in self.whitelist for x in toolchain_wl) or
                           not any(x in self.whitelist for x in toolchain_wl))
        assert toolchain_wl_ok, 'internal error: bad toolchain whitelist'

        self.do_register(parser)

    def _prep_use_prebuilt(self):
        if self.arguments.zephyr_gcc_variant == 'gccarmemb':
            self._prep_use_prebuilt_gccarmemb()

    def _prep_use_prebuilt_gccarmemb(self):
        gccarmemb = find_arm_none_eabi_gcc()
        self.make_overrides['gccarmemb_toolchain_path'] = gccarmemb

    def prep_for_run(self):
        '''Finish setting up arguments and prepare run environments.

        Clean up the representation of some arguments, add values for
        'pseudo-arguments' that don't have options yet, and retrieve build
        environments to run commands in.

        If '--outputs' was whitelisted, it is assumed this command is
        invoking make, and the instance variable 'make_envs' will be set
        upon return.  It will contain the keys 'app' and 'mcuboot' as
        appropriate, and values equal to the build environments to use
        for those outputs.  These environments have BOARD unset when
        --board is whitelisted, but otherwise have the same value as the
        parent environment.'''
        base_env = dict(os.environ)
        self.do_prep_for_run(base_env)

        if '--board' in self.whitelist:
            if len(self.arguments.boards) == 0:
                self.arguments.boards = [BOARD_DEFAULT]
            if 'BOARD' in base_env:
                if [base_env['BOARD']] != self.arguments.boards:
                    self.wrn('Ignoring BOARD={}: targeting {}'.format(
                        base_env['BOARD'], self.arguments.boards))
                del base_env['BOARD']

        if '--outputs' in self.whitelist:
            if self.arguments.outputs == 'all':
                self.arguments.outputs = BUILD_OUTPUTS
            else:
                self.arguments.outputs = [self.arguments.outputs]

            # Set up overridden variables.
            self.make_overrides = {'zephyr_base': find_zephyr_base()}
            if self.arguments.prebuilt_toolchain.startswith('y'):
                self._prep_use_prebuilt()
            for arg in BUILD_OPTIONS:
                val = getattr(self.arguments, arg)
                self.make_overrides[arg] = val

            # Create the application and mcuboot build environments,
            # warning when environment variables are overridden.
            app_build_env = dict(base_env)
            for arg, val in self.make_overrides.items():
                env_var = self.arg_to_env_var(arg)
                self._wrn_if_overridden(base_env, env_var, val)
                app_build_env[env_var] = val
            mcuboot_build_env = dict(app_build_env)
            del mcuboot_build_env['CONF_FILE']

            envs = {'app': app_build_env, 'mcuboot': mcuboot_build_env}
            self.make_envs = {k: v for k, v in envs.items()
                              if k in self.arguments.outputs}

    def invoke(self, arguments):
        '''Invoke the command, with given arguments.'''
        self.arguments = arguments
        self.prep_for_run()
        self.do_invoke()

    #
    # Miscellaneous
    #

    def arg_to_env_var(self, arg):
        return arg.upper()

    def _wrn_if_overridden(self, env, env_var, val):
        if env_var not in env or val == env[env_var]:
            return
        env_val = env[env_var]
        self.wrn('Warning: overriding {}:'.format(env_var))
        self.wrn('\tenvironment value: {}'.format(env_val))
        self.wrn('\toverridden to:     {}'.format(val))


#
# Build
#


class Build(Command):

    def __init__(self, *args, **kwargs):
        kwargs['whitelist'] = None
        super(Build, self).__init__(*args, **kwargs)

    @property
    def command_name(self):
        return 'build'

    @property
    def command_help(self):
        return 'Build application images'

    def arg_help(self, argument):
        if argument == '--outputs':
            return 'Which outputs to build (default: all)'
        return super(Build, self).arg_help(argument)

    def do_register(self, parser):
        parser.add_argument('-K', '--signing-key',
                            help='''Path to signing key for application
                                 binary. WARNING: if not given, an INSECURE
                                 default key is used which should NOT be
                                 used for production images.''')
        parser.add_argument('-V', '--imgtool-version',
                            help='''Image version in X.Y.Z semantic
                                 versioning format (default: {})'''.format(
                                     MCUBOOT_IMGTOOL_VERSION_DEFAULT))
        parser.add_argument('--skip-signature',
                            action='store_true',
                            help="""If set, don't sign the resulting binary
                                 for loading by mcuboot. Use of this option
                                 implies -o app, and is incompatible with
                                 the -K option.""")

    def do_prep_for_run(self, environment):
        self.insecure_requested = False
        if self.arguments.skip_signature:
            if self.arguments.signing_key is not None:
                raise ValueError('{} is incompatible with {}'.format(
                    '--skip-signature', '--signing-key'))
            self.arguments.outputs = 'app'
        else:
            for b in self.arguments.boards:
                if b not in MCUBOOT_WORD_SIZES:
                    raise ValueError("{}: unknown flash word size".format(b))
        if self.arguments.signing_key is None:
            self.arguments.signing_key = os.path.join(find_mcuboot_root(),
                                                      MCUBOOT_DEV_KEY)
            self.insecure_requested = True
        if self.arguments.imgtool_version is None:
            default = MCUBOOT_IMGTOOL_VERSION_DEFAULT
            self.wrn('No --imgtool-version given, using {}'.format(default))
            self.arguments.imgtool_version = default
        if not self.version_is_semver(self.arguments.imgtool_version):
            raise ValueError('{} is not in semantic versioning format'.format(
                self.arguments.imgtool_version))

    def do_invoke(self):
        mcuboot = find_mcuboot_root()

        # Run the builds.
        for board in self.arguments.boards:
            for app in self.arguments.app:
                app = app.rstrip(os.path.sep)
                makefile_dirs = {'app': find_app_root(app), 'mcuboot': mcuboot}
                for output in self.arguments.outputs:
                    self.do_build(board, app, output, makefile_dirs[output])

                # Only generate a flashing script if we've built both outputs.
                if self.arguments.outputs != ['app', 'mcuboot']:
                    continue
                outdir = self.arguments.outdir
                debug = self.arguments.debug
                flasher = ZephyrBinaryFlasher.create_flasher(board, app,
                                                             outdir, debug)
                flasher.generate_script('sh')

    def do_build(self, board, app, output, makefile_dir):
        signing_app = (output == 'app' and not self.arguments.skip_signature)
        outdir = find_app_outdir(self.arguments.outdir, app, board, output)

        # Application/mcuboot build command.
        #
        # The Zephyr build's output exports are useful during the build
        # for signing images, and also afterwards, e.g. when deciding
        # how to flash the binaries.
        cmd_build = ['make',
                     '-C', shlex.quote(makefile_dir),
                     '-j', str(self.arguments.jobs),
                     'O={}'.format(shlex.quote(outdir))]
        cmd_exports = cmd_build + ['outputexports']
        build_env = dict(self.make_envs[output])
        build_env['BOARD'] = board

        try:
            self.dbg_make_cmd('Building {} image'.format(output),
                              cmd_build, env=build_env, board=board)
            subprocess.check_call(cmd_build, env=build_env)
            self.dbg_make_cmd('Generating outputexports',
                              cmd_exports, env=build_env, board=board)
            subprocess.check_call(cmd_exports, env=build_env)
            # Note: generating the signing command requires some Zephyr
            # build outputs.
            if signing_app:
                cmd_sign = self.sign_command(board, app, outdir)
                self.dbg('Signing application binary:')
                self.dbg('\t' + ' '.join(cmd_sign))
                subprocess.check_call(cmd_sign, env=build_env)
                if self.insecure_requested:
                    self.wrn('Warning: used insecure default signing key.',
                             'IMAGES ARE NOT SUITABLE FOR PRODUCTION USE.')
        except subprocess.CalledProcessError as e:
            if not self.arguments.keep_going:
                raise

    def sign_command(self, board, app, outdir):
        exports = ZephyrExports(outdir)
        vtoff = exports.get_ensure_hex('CONFIG_TEXT_SECTION_OFFSET')
        pad = exports.get_ensure_hex('FLASH_AREA_IMAGE_0_SIZE')
        unsigned_bin = os.path.join(outdir, 'zephyr.bin')
        app_base = os.path.basename(app)
        app_bin_name = '{}-{}-signed.bin'.format(app_base, board)
        signed_bin = os.path.join(outdir, app_bin_name)
        version = self.arguments.imgtool_version
        return ['/usr/bin/env', 'python3',
                os.path.join(find_mcuboot_root(), MCUBOOT_IMGTOOL),
                'sign',
                '--key', shlex.quote(self.arguments.signing_key),
                '--align', MCUBOOT_WORD_SIZES[board],
                '--header-size', vtoff,
                '--included-header',
                '--pad', pad,
                '--version', shlex.quote(version),
                shlex.quote(unsigned_bin),
                shlex.quote(signed_bin)]

    def version_is_semver(self, version):
        return re.match('^\d+[.]\d+[.]\d+$', version) is not None


#
# Configure
#


class Configure(Command):

    def __init__(self, *args, **kwargs):
        kwargs['whitelist'] = None
        super(Configure, self).__init__(*args, **kwargs)

    @property
    def command_name(self):
        return 'configure'

    @property
    def command_help(self):
        return '''Configure application images. If multiple apps
               are given, the configurators are run in the order the apps
               are specified.'''

    def do_register(self, parser):
        default = CONFIGURATOR_DEFAULT
        parser.add_argument(
            '-C', '--configurator',
            choices=CONFIGURATORS,
            default=default,
            help='''Configure front-end (default: {})'''.format(default))

    def do_invoke(self):
        mcuboot = find_mcuboot_root()

        for board in self.arguments.boards:
            for app in self.arguments.app:
                makefile_dirs = {'app': find_app_root(app), 'mcuboot': mcuboot}
                for output in self.arguments.outputs:
                    self.do_configure(board, app, output,
                                      makefile_dirs[output])

    def do_configure(self, board, app, output, makefile_dir):
        outdir = find_app_outdir(self.arguments.outdir, app, board, output)
        cmd_configure = ['make',
                         '-C', shlex.quote(makefile_dir),
                         '-j', str(self.arguments.jobs),
                         'O={}'.format(shlex.quote(outdir)),
                         self.arguments.configurator]
        configure_env = dict(self.make_envs[output])
        configure_env['BOARD'] = board

        try:
            self.dbg_make_cmd('Configuring {} for {}'.format(output, app),
                              cmd_configure, env=configure_env, board=board)
            subprocess.check_call(cmd_configure, env=configure_env)
        except subprocess.CalledProcessError as e:
            if not self.arguments.keep_going:
                sys.exit(e.returncode)


#
# Flash
#


class Flash(Command):

    def __init__(self, *args, **kwargs):
        kwargs['whitelist'] = {'--board', '--outdir', 'app'}
        super(Flash, self).__init__(*args, **kwargs)

    @property
    def command_name(self):
        return 'flash'

    @property
    def command_help(self):
        return 'Flash a bootloader and a signed application image to a board.'

    def do_register(self, parser):
        parser.add_argument('-d', '--device-id', dest='device_ids',
                            default=[], action='append',
                            help='''Device identifier given to the flashing
                                  tool. Should only be used with one board
                                  target. This may be given multiple times
                                  to target additional devices.''')
        parser.add_argument('-e', '--extra', default='',
                            help='''Extra arguments to pass to the
                                 flashing tool''')

    def do_prep_for_run(self, environment):
        if len(self.arguments.app) > 1:
            raise ValueError('only one application may be flashed at a time.')
        if self.arguments.device_ids and len(self.arguments.boards) > 1:
            raise ValueError('only one board target may be used when '
                             'specifying device ids')

        self.arguments.app = self.arguments.app[0].rstrip(os.path.sep)
        self.arguments.extra = self.arguments.extra.split()

    def do_invoke(self):
        if self.arguments.device_ids:
            for device_id in self.arguments.device_ids:
                flasher = ZephyrBinaryFlasher.create_flasher(
                    self.arguments.boards[0], self.arguments.app,
                    self.arguments.outdir, self.arguments.debug)
                flasher.flash(device_id, self.arguments.extra)
        else:
            for board in self.arguments.boards:
                flasher = ZephyrBinaryFlasher.create_flasher(
                    board, self.arguments.app, self.arguments.outdir,
                    self.arguments.debug)
                flasher.flash(None, self.arguments.extra)


#
# main()
#


def main():
    # Parsing is split into a multilevel structure based on the top-level
    # command. The first level is $scriptname [-h] $command [command_arg ...]
    top_parser = argparse.ArgumentParser()
    top_parser.add_argument('--debug', default=False, action='store_true',
                            help='If set, print extra debugging information.')
    cmd_parsers = top_parser.add_subparsers(help='Command', dest='cmd')

    command_handlers = {}
    for sub_cls in Command.__subclasses__():
        command = sub_cls()
        command.register(cmd_parsers)
        command_handlers[command.command_name] = command

    args = top_parser.parse_args()
    if args.cmd is None:
        commands = ', '.join(command_handlers.keys())
        print('Missing command. Choices: {}'.format(commands), file=sys.stderr)
        sys.exit(1)
    try:
        command_handlers[args.cmd].invoke(args)
    except Exception as e:
        if args.debug:
            raise
        else:
            re_run = '"{} --debug {} ..."'.format(PROGRAM, args.cmd)
            print('Error: {}'.format(e), file=sys.stderr)
            print('Re-run as {} for a stack trace.'.format(re_run),
                  file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
