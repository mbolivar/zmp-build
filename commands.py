# Copyright (c) 2017 Linaro Limited.
# Copyright (c) 2017 Open Source Foundries Limited.
#
# SPDX-License-Identifier: Apache-2.0

import abc
import multiprocessing
import os
import re
import shlex
import subprocess
import sys

from core import find_default_outdir, find_zephyr_base, \
                 find_arm_none_eabi_gcc, find_mcuboot_root, \
                 find_app_root, find_app_outdir

from exports import ZephyrExports
from flashers import ZephyrBinaryFlasher


# Default values shared by multiple commands.
BOARD_DEFAULT = 'nrf52_blenano2'
ZEPHYR_GCC_VARIANT_DEFAULT = 'gccarmemb'
CONF_FILE_DEFAULT = 'prj.conf'
BUILD_PARALLEL_DEFAULT = multiprocessing.cpu_count()

# What types of build outputs to produce.
# - app: ZMP-ready application, which can be signed for flashing or FOTA.
# - mcuboot: ZMP bootloader, not signed and must be flashed.
BUILD_OUTPUTS = ['app', 'mcuboot']

# Build configuration from command line options that overrides environment
# variables. Note that BOARD is special, since we can target multiple boards in
# one script invocation, so we don't include it in this list.
BUILD_OPTIONS = ['conf_file',
                 'zephyr_gcc_variant',
                 # 'board',
                 ]

# Programs which 'configure' can use to generate Zephyr .config files.
CONFIGURATORS = ['config', 'nconfig', 'menuconfig', 'xconfig', 'gconfig',
                 'oldconfig', 'silentoldconfig', 'defconfig', 'savedefconfig',
                 'allnoconfig', 'allyesconfig', 'alldefconfig', 'randconfig',
                 'listnewconfig', 'olddefconfig']
# menuconfig is portable and the one most examples are based off of.
CONFIGURATOR_DEFAULT = 'menuconfig'

# Development-only firmware binary signing key.
MCUBOOT_DEV_KEY = 'root-rsa-2048.pem'
# Version to write to signed binaries when none is specified.
MCUBOOT_IMGTOOL_VERSION_DEFAULT = '0.0.0'
# imgtool.py state. This post-processes binaries for chain-loading by mcuboot.
MCUBOOT_IMGTOOL = os.path.join('scripts', 'imgtool.py')


class Command(abc.ABC):
    '''Parent class for runnable commands.'''

    HELP = {
        # Generally useful.
        '--board': '''Zephyr board to target (default: {}). This may be
                   given multiple times to target additional boards.'''.format(
                       BOARD_DEFAULT),
        '--outdir': '''Build directory (default: '{}' under ZMP
                    root).'''.format(find_default_outdir()),
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
        '--jobs': '''Number of jobs to run simultaneously (the default is
                  derived from the number of available CPUs)''',
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
        used as a starting point for self.build_envs, if that is created
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

    def dbg_zephyr_build(self, msg, cmd, env=None, board=None):
        '''Debug helper for use when invoking the Zephyr build system.'''
        if self.arguments.debug:
            self.dbg('{}:'.format(msg))
            if board is not None:
                self.dbg('\tBOARD={} \\'.format(board))
            if env is not None:
                for arg in self.build_overrides:
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

        # These are needed by commands which invoke the Zephyr build
        # system ('build' and 'configure').
        #
        # TODO: determine which of these are still relevant to CMake.
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
        self.build_overrides['gccarmemb_toolchain_path'] = gccarmemb

    def prep_for_run(self):
        '''Finish setting up arguments and prepare run environments.

        Clean up the representation of some arguments, add values for
        'pseudo-arguments' that don't have options yet, and retrieve build
        environments to run commands in.

        If '--outputs' was whitelisted, it is assumed this command is
        building binaries, and the instance variable 'build_envs' will be set
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
            self.build_overrides = {'zephyr_base': find_zephyr_base()}
            if self.arguments.prebuilt_toolchain.startswith('y'):
                self._prep_use_prebuilt()
            for arg in BUILD_OPTIONS:
                val = getattr(self.arguments, arg)
                self.build_overrides[arg] = val

            # Create the application and mcuboot build environments,
            # warning when environment variables are overridden.
            app_build_env = dict(base_env)
            for arg, val in self.build_overrides.items():
                env_var = self.arg_to_env_var(arg)
                self._wrn_if_overridden(base_env, env_var, val)
                app_build_env[env_var] = val
            mcuboot_build_env = dict(app_build_env)
            del mcuboot_build_env['CONF_FILE']

            envs = {'app': app_build_env, 'mcuboot': mcuboot_build_env}
            self.build_envs = {k: v for k, v in envs.items()
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
                source_dirs = {'app': find_app_root(app), 'mcuboot': mcuboot}
                for output in self.arguments.outputs:
                    self.do_build(board, app, output, source_dirs[output])

    def do_build(self, board, app, output, source_dir):
        signing_app = (output == 'app' and not self.arguments.skip_signature)
        outdir = find_app_outdir(self.arguments.outdir, app, board, output)

        # Application/mcuboot build command.
        #
        # The Zephyr build's output exports are useful during the build
        # for signing images, and also afterwards, e.g. when deciding
        # how to flash the binaries.
        cmd_build = ['make',
                     '-C', shlex.quote(source_dir),
                     '-j', str(self.arguments.jobs),
                     'O={}'.format(shlex.quote(outdir))]
        cmd_exports = cmd_build + ['outputexports']
        build_env = dict(self.build_envs[output])
        build_env['BOARD'] = board

        try:
            self.dbg_zephyr_build('Building {} image'.format(output),
                                  cmd_build, env=build_env, board=board)
            subprocess.check_call(cmd_build, env=build_env)
            self.dbg_zephyr_build('Generating outputexports',
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
            raise

    def sign_command(self, board, app, outdir):
        exports = ZephyrExports(outdir)
        align = exports.get_ensure_int('FLASH_WRITE_BLOCK_SIZE')
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
                '--align', align,
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
                source_dirs = {'app': find_app_root(app), 'mcuboot': mcuboot}
                for output in self.arguments.outputs:
                    self.do_configure(board, app, output,
                                      source_dirs[output])

    def do_configure(self, board, app, output, source_dir):
        outdir = find_app_outdir(self.arguments.outdir, app, board, output)
        cmd_configure = ['make',
                         '-C', shlex.quote(source_dir),
                         '-j', str(self.arguments.jobs),
                         'O={}'.format(shlex.quote(outdir)),
                         self.arguments.configurator]
        configure_env = dict(self.build_envs[output])
        configure_env['BOARD'] = board

        try:
            self.dbg_zephyr_build('Configuring {} for {}'.format(output, app),
                                  cmd_configure, env=configure_env,
                                  board=board)
            subprocess.check_call(cmd_configure, env=configure_env)
        except subprocess.CalledProcessError as e:
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
