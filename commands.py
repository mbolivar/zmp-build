# Copyright (c) 2017 Linaro Limited.
# Copyright (c) 2017 Open Source Foundries Limited.
#
# SPDX-License-Identifier: Apache-2.0

import abc
import glob
import multiprocessing
import os
import re
import shlex
import subprocess
import sys

from core import find_default_outdir, find_zephyr_base, \
                 find_arm_none_eabi_gcc, find_mcuboot_root, \
                 find_app_root, find_app_outdir, check_boards, \
                 check_dependencies


# Default values shared by multiple commands.
BOARD_DEFAULT = 'nrf52_blenano2'
ZEPHYR_GCC_VARIANT_DEFAULT = 'gccarmemb'
BUILD_PARALLEL_DEFAULT = multiprocessing.cpu_count()

# What types of build outputs to produce.
# - app: ZMP-ready application, which can be signed for flashing or FOTA.
# - mcuboot: ZMP bootloader, not signed and must be flashed.
BUILD_OUTPUTS = ['app', 'mcuboot']

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


class BuildConfiguration:
    '''This helper class provides access to build-time configuration.

    Configuration options can be read as if the object were a dict,
    either object['CONFIG_FOO'] or object.get('CONFIG_FOO').

    Configuration values in auto.conf and generated_dts_board.conf are
    available.'''

    def __init__(self, build_dir):
        self.build_dir = build_dir
        self.options = {}
        self._init()

    def __getitem__(self, item):
        return self.options[item]

    def get(self, option, *args):
        return self.options.get(option, *args)

    def _init(self):
        zephyr = os.path.join(self.build_dir, 'zephyr')
        generated = os.path.join(zephyr, 'include', 'generated')
        files = [os.path.join(generated, 'generated_dts_board.conf'),
                 os.path.join(zephyr, '.config')]
        for f in files:
            self._parse(f)

    def _parse(self, filename):
        with open(filename, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                option, value = line.split('=', 1)
                self.options[option] = self._parse_value(value)

    def _parse_value(self, value):
        if value.startswith('"') or value.startswith("'"):
            return value.split()
        try:
            return int(value, 0)
        except ValueError:
            return value


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
        '--conf-file': 'If given, sets app (not mcuboot) configuration file',
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

    def do_prep_for_run(self):
        '''Subclasses may override to register a prep_for_run() callback.

        When this method is invoked, self.arguments contains the
        command's arguments.'''
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
            parser.add_argument('-c', '--conf-file',
                                help=self.arg_help('--conf-file'))
        if '--jobs' in self.whitelist:
            parser.add_argument('-j', '--jobs',
                                type=int, default=BUILD_PARALLEL_DEFAULT,
                                help=self.arg_help('--jobs'))
        if '--outputs' in self.whitelist:
            parser.add_argument('-o', '--outputs',
                                choices=BUILD_OUTPUTS + ['all'], default='all',
                                help=self.arg_help('--outputs'))

        self.do_register(parser)

    def prep_for_run(self):
        '''Finish setting up arguments and prepare run environment.

        Clean up the representation of some arguments, add values for
        'pseudo-arguments' that don't have options yet, and create
        environment to run commands in.

        The instance variable 'command_env' will be set upon return.
        It will be used when running commands with check_call().'''
        self.do_prep_for_run()
        command_env = dict(os.environ)

        if '--board' in self.whitelist:
            if len(self.arguments.boards) == 0:
                self.arguments.boards = [BOARD_DEFAULT]
            if 'BOARD' in command_env:
                if [command_env['BOARD']] != self.arguments.boards:
                    self.wrn('Ignoring BOARD={}: targeting {}'.format(
                        command_env['BOARD'], self.arguments.boards))
                del command_env['BOARD']

        if '--outputs' in self.whitelist:
            if self.arguments.outputs == 'all':
                self.arguments.outputs = BUILD_OUTPUTS
            else:
                self.arguments.outputs = [self.arguments.outputs]

        # Override ZEPHYR_BASE to the microPlatform tree. External
        # trees might not have the zmP patches.
        zephyr_base = find_zephyr_base()
        self.override_warn(command_env, 'ZEPHYR_BASE', zephyr_base)
        command_env['ZEPHYR_BASE'] = zephyr_base

        self.command_env = command_env

    def invoke(self, arguments):
        '''Invoke the command, with given arguments.'''
        self.arguments = arguments
        self.prep_for_run()
        self.do_invoke()

    #
    # Miscellaneous
    #

    def _cmd_to_string(self, command):
        fmt = ' '.join('{}' for _ in command)
        args = [shlex.quote(s) for s in command]
        return fmt.format(*args)

    def check_call(self, command, **kwargs):
        msg = kwargs.get('msg', 'Running command')
        env = self.command_env

        if self.arguments.debug:
            self.dbg('{}:'.format(msg))
            self.dbg('\tZEPHYR_BASE={}'.format(env['ZEPHYR_BASE']))
            if 'cwd' in kwargs:
                self.dbg('\tcwd: {}'.format(kwargs['cwd']))
            self.dbg('\t{}'.format(self._cmd_to_string(command)))

        kwargs['env'] = self.command_env
        try:
            subprocess.check_call(command, **kwargs)
        except subprocess.CalledProcessError:
            cmd = self._cmd_to_string(command)
            print('Failed to run command: {}'.format(cmd), file=sys.stderr)
            raise

    def remove_env(self, env, env_var, val):
        if env_var in env:
            self.override_warn(env, env_var, val)
            del env[env_var]

    def override_env(self, env, env_var, val):
        if env_var in env:
            self.override_warn(env, env_var, val)
        env[env_var] = val

    def override_warn(self, env, env_var, val):
        if env_var not in env:
            return
        env_val = env[env_var]
        if env_val == val:
            return
        self.wrn('Warning: overriding {}:'.format(env_var))
        self.wrn('\tenvironment value: {}'.format(env_val))
        self.wrn('\tusing value:       {}'.format(val))


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

    def do_prep_for_run(self):
        if self.arguments.skip_signature:
            if self.arguments.signing_key is not None:
                raise ValueError('{} is incompatible with {}'.format(
                    '--skip-signature', '--signing-key'))
            self.arguments.outputs = 'app'

        if self.arguments.signing_key is None:
            key = os.path.join(find_mcuboot_root(), MCUBOOT_DEV_KEY)
            self.insecure_requested = True
        else:
            key = self.arguments.signing_key
            self.insecure_requested = False
        self.arguments.signing_key = os.path.abspath(key)

        if self.arguments.imgtool_version is None:
            default = MCUBOOT_IMGTOOL_VERSION_DEFAULT
            self.wrn('No --imgtool-version given, using {}'.format(default))
            self.arguments.imgtool_version = default
        if not self.version_is_semver(self.arguments.imgtool_version):
            raise ValueError('{} is not in semantic versioning format'.format(
                self.arguments.imgtool_version))
        check_boards(self.arguments.boards)
        check_dependencies(['cmake', 'make', 'dtc'])

    def do_invoke(self):
        mcuboot = find_mcuboot_root()
        mcuboot_app_source = os.path.join(mcuboot, 'boot', 'zephyr')
        gcc_variant = self.arguments.zephyr_gcc_variant

        # For now, configure prebuilt toolchains through the environment.
        if ('--prebuilt-toolchain' in self.whitelist and
                self.arguments.prebuilt_toolchain.startswith('y')):
            if gcc_variant == 'gccarmemb':
                gccarmemb = find_arm_none_eabi_gcc()
                self.override_env(self.command_env, 'GCCARMEMB_TOOLCHAIN_PATH',
                                  gccarmemb)
            else:
                raise NotImplementedError(
                    "no prebuilts available for {}".format(gcc_variant))

        # Warn once on a GCC variant override.
        self.override_warn(self.command_env, 'ZEPHYR_GCC_VARIANT', gcc_variant)

        # Prepare the host tools and prepend them to the build
        # environment path. These are available on Linux via the
        # Zephyr SDK, but that may not be installed, and is not
        # helpful on OS X.
        self.prepare_host_tools()

        # Run the builds.
        for board in self.arguments.boards:
            for app in self.arguments.app:
                app = app.rstrip(os.path.sep)
                source_dirs = {'app': find_app_root(app),
                               'mcuboot': mcuboot_app_source}
                for output in self.arguments.outputs:
                    self.do_build(board, app, output, source_dirs[output])

    def prepare_host_tools(self):
        host_tools = os.path.join(find_zephyr_base(), 'scripts')
        outdir = os.path.join(self.arguments.outdir, 'zephyr', 'scripts')

        # Ensure the output directory exists.
        os.makedirs(outdir, exist_ok=True)

        # If cmake has been called successfully to initialize the
        # output directory, then just rebuild the host
        # tools. Otherwise, run cmake before building.
        if 'Makefile' not in os.listdir(outdir):
            cmd_generate = (['cmake',
                             '-G{}'.format('Unix Makefiles'),
                             shlex.quote(host_tools)])
            self.check_call(cmd_generate, cwd=outdir)
        cmd_build = (['cmake',
                      '--build', shlex.quote(outdir),
                      '--',
                      '-j{}'.format(self.arguments.jobs)])
        self.check_call(cmd_build, cwd=outdir)

        # Monkey-patch the path to add the output directory.
        # TODO: windows?
        out_path = os.path.join(outdir, 'kconfig')
        path_env_val = self.command_env['PATH']
        self.command_env['PATH'] = os.pathsep.join([out_path, path_env_val])

    def do_build(self, board, app, output, source_dir):
        signing_app = (output == 'app' and not self.arguments.skip_signature)
        outdir = find_app_outdir(self.arguments.outdir, app, board, output)
        verbose = ['VERBOSE=1'] if self.arguments.debug else []
        conf_file = (['-DCONF_FILE={}'.format(self.arguments.conf_file)]
                     if output == 'app' and self.arguments.conf_file else [])
        gcc_variant = self.arguments.zephyr_gcc_variant

        # Ensure the output directory exists.
        os.makedirs(outdir, exist_ok=True)

        # If cmake has been called successfully to initialize the
        # output directory, then just rebuild. Otherwise, run cmake
        # before rebuilding.
        if 'Makefile' not in os.listdir(outdir):
            cmd_generate = (
                ['cmake',
                 '-DBOARD={}'.format(shlex.quote(board)),
                 '-DZEPHYR_GCC_VARIANT={}'.format(shlex.quote(gcc_variant)),
                 '-G{}'.format('Unix Makefiles')] +
                conf_file +
                [shlex.quote(source_dir)])
            self.check_call(cmd_generate, cwd=outdir)
        cmd_build = (['cmake',
                      '--build', shlex.quote(outdir),
                      '--',
                      '-j{}'.format(self.arguments.jobs)] +
                     verbose)
        self.check_call(cmd_build, cwd=outdir)

        # Generate the command needed to sign the application. Note:
        # generating the signing command requires some Zephyr build
        # outputs, so this has to come after.
        if signing_app:
            cmd_sign = self.sign_command(board, app, outdir)
            self.check_call(cmd_sign, cwd=outdir)
            if self.insecure_requested:
                self.wrn('Warning: used insecure default signing key.',
                         'IMAGES ARE NOT SUITABLE FOR PRODUCTION USE.')

    def sign_command(self, board, app, outdir):
        bcfg = BuildConfiguration(outdir)
        align = str(bcfg['FLASH_WRITE_BLOCK_SIZE'])
        vtoff = str(bcfg['CONFIG_TEXT_SECTION_OFFSET'])
        pad = str(bcfg['FLASH_AREA_IMAGE_0_SIZE'])
        unsigned_bin = os.path.join(outdir, 'zephyr', 'zephyr.bin')
        app_base = os.path.basename(app)
        app_bin_name = '{}-{}-signed.bin'.format(app_base, board)
        signed_bin = os.path.join(outdir, 'zephyr', app_bin_name)
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
        cmd_configure = ['cmake',
                         '--build', shlex.quote(outdir),
                         '--target', self.arguments.configurator]
        self.check_call(cmd_configure)


#
# Flash
#


class Flash(Command):

    def __init__(self, *args, **kwargs):
        kwargs['whitelist'] = {'--board', '--outdir', 'app', '--outputs'}
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
                            help='''This command has been temporarily
                            disabled, and will cause an error if used.''')
        parser.add_argument('-e', '--extra', default='',
                            help='''This command has been temporarily
                            disabled, and will cause an error if used.''')

    def do_prep_for_run(self):
        if len(self.arguments.app) > 1:
            raise ValueError('only one application may be flashed at a time.')
        if self.arguments.device_ids and len(self.arguments.boards) > 1:
            raise ValueError('only one board target may be used when '
                             'specifying device ids')

        if self.arguments.device_ids or self.arguments.extra:
            # FIXME: try to convert these to use CMake. For now, since
            # they require passing values at runtime, just disable them.
            msg = ('--device-id and --extra are temporarily unavailable. '
                   'They will be restored if possible.')
            raise NotImplementedError(msg)

        self.arguments.app = self.arguments.app[0].rstrip(os.path.sep)
        self.arguments.extra = self.arguments.extra.split()

    def do_invoke(self):
        verbose = ['--', 'VERBOSE=1'] if self.arguments.debug else []
        outdir = self.arguments.outdir
        app = self.arguments.app

        # TEMPHACK: we don't have a good way to pass dynamic
        # information to CMake targets. Just hack it for now by
        # passing it through a fresh environment
        hack_app_env = dict(self.command_env)

        for board in self.arguments.boards:
            mcuboot_outdir = find_app_outdir(outdir, app, board, 'mcuboot')
            app_outdir = find_app_outdir(outdir, app, board, 'app')
            cmd_flash_mcuboot = (
                ['cmake',
                 '--build', shlex.quote(mcuboot_outdir),
                 '--target', 'flash'] +
                verbose)
            cmd_flash_app = (
                ['cmake',
                 '--build', shlex.quote(app_outdir),
                 '--target', 'flash'] +
                verbose)

            if 'mcuboot' in self.arguments.outputs:
                self.check_call(cmd_flash_mcuboot, cwd=mcuboot_outdir)

            if 'app' in self.arguments.outputs:
                hack_bin = glob.glob(os.path.join(app_outdir, 'zephyr',
                                                  '*-signed.bin'))[0]
                hack_app_env['ZEPHYR_HACK_OVERRIDE_BIN'] = hack_bin
                subprocess.check_call(cmd_flash_app, cwd=app_outdir,
                                      env=hack_app_env)
