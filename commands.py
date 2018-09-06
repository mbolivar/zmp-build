# Copyright (c) 2017 Linaro Limited.
# Copyright (c) 2017 Open Source Foundries Limited.
# Copyright (c) 2018 Foundries.io Limited.
#
# SPDX-License-Identifier: Apache-2.0

import abc
import importlib
import multiprocessing
import os
import re
import shlex
import subprocess
import sys

from core import find_default_outdir, find_zephyr_base, \
                 find_arm_none_eabi_gcc, \
                 find_mcuboot_root, find_mcuboot_outdir, \
                 find_app_root, find_app_outdir, check_boards, \
                 check_dependencies, find_sdk_build_root


# Default values shared by multiple commands.
BOARD_DEFAULT = 'nrf52_blenano2'
ZEPHYR_TOOLCHAIN_VARIANT_DEFAULT = 'gnuarmemb'
BUILD_PARALLEL_DEFAULT = multiprocessing.cpu_count()

# What types of build outputs to produce.
# - app: ZMP-ready application, which can be signed for flashing or FOTA.
# - mcuboot: ZMP bootloader, not signed and must be flashed.
BUILD_OUTPUTS = ['app', 'mcuboot']

# Programs which 'configure' can use to generate Zephyr .config files.
CONFIGURATORS = ['menuconfig']
# menuconfig is portable and the one most examples are based off of.
CONFIGURATOR_DEFAULT = 'menuconfig'

# Development-only firmware binary signing key.
MCUBOOT_DEV_KEY = 'root-rsa-2048.pem'
# Version to write to signed binaries when none is specified.
MCUBOOT_IMGTOOL_VERSION_DEFAULT = '0.0.0+0'
# imgtool.py state. This post-processes binaries for chain-loading by mcuboot.
MCUBOOT_IMGTOOL = os.path.join('scripts', 'imgtool.py')

# Any globally desirable CMake options can be added here.
CMAKE_OPTIONS = []

# Help format strings for options shared by multiple commands.
HELP = {
    '--board': '''Zephyr board to target (default: {}). This may be
               given multiple times to target additional boards.'''.format(
                   BOARD_DEFAULT),
    '--outdir': '''build directory (default: '{}').'''.format(
        find_default_outdir()),
    '--outputs': 'which outputs to {} (default: all)',
    'app': 'application(s) sources',
}


def signed_app_name(app, board, app_outdir, ext):
    app_base = os.path.basename(app)
    app_fmt = '{}-{}-signed.{}'
    file_name = app_fmt.format(app_base, board, ext)
    path = os.path.join(app_outdir, 'zephyr', file_name)
    return path


class Command(abc.ABC):
    '''Parent class for runnable commands.'''

    def __init__(self, stdout=sys.stdout, stderr=sys.stderr):
        '''Create a new command object.

        This doesn't actually register a command; that's done just by
        creating a Command subclass. This creates an individual
        instance for use, and optionally redirects its output streams.'''
        self.stdout = stdout
        self.stderr = stderr

    #
    # Abstract interfaces and overridable behavior.
    #

    @abc.abstractproperty
    def command_name(self):
        '''The name of this command as invoked by users.'''

    @abc.abstractproperty
    def command_help(self):
        '''The top-level help string for this command to display to users.'''

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

        Any subclass-specific commands should be added via the
        do_register() hook.'''
        parser = parsers.add_parser(self.command_name, help=self.command_help)
        self.do_register(parser)

    def prep_for_run(self):
        '''Finish setting up arguments and prepare run environment.

        Clean up the representation of some arguments, add values for
        'pseudo-arguments' that don't have options yet, and create
        environment to run commands in.

        The instance variable 'command_env' will be set upon return.
        It will be used when running commands with check_call().'''
        # Override ZEPHYR_BASE to the microPlatform's tree.
        zephyr_base = find_zephyr_base()
        env_val = os.environ.get('ZEPHYR_BASE')
        if env_val is not None and env_val != zephyr_base:
            self.wrn('Warning: overriding ZEPHYR_BASE:')
            self.wrn('\tenvironment value: {}'.format(env_val))
            self.wrn('\tusing value:       {}'.format(zephyr_base))

        # Ensure we can import the west runner subpackage from the
        # current ZEPHYR_BASE, either in this script or when invoking
        # west programmatically.
        sys.path.append(os.path.join(zephyr_base, 'scripts', 'meta'))

        # For some reason, relative imports of runner submodules on
        # Python <3.6 fail with errors about west.runner not being
        # imported without this line.
        if sys.version_info < (3, 6):
            importlib.import_module('west.runner')

        self.do_prep_for_run()
        command_env = dict(os.environ)
        command_env['ZEPHYR_BASE'] = zephyr_base
        self.command_env = command_env

        if len(self.arguments.boards) == 0:
            self.arguments.boards = [BOARD_DEFAULT]
        if 'BOARD' in command_env:
            if [command_env['BOARD']] != self.arguments.boards:
                self.wrn('Ignoring BOARD={}: targeting {}'.format(
                    command_env['BOARD'], self.arguments.boards))
            del command_env['BOARD']

        if self.arguments.outputs == 'all':
            self.arguments.outputs = BUILD_OUTPUTS
        else:
            self.arguments.outputs = [self.arguments.outputs]

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
        env = kwargs.get('env', self.command_env)

        if self.arguments.debug:
            self.dbg('{}:'.format(msg))
            self.dbg('\tZEPHYR_BASE={}'.format(env['ZEPHYR_BASE']))
            if 'cwd' in kwargs:
                self.dbg('\tcwd: {}'.format(kwargs['cwd']))
            self.dbg('\t{}'.format(self._cmd_to_string(command)))

        kwargs['env'] = env
        try:
            subprocess.check_call(command, **kwargs)
        except subprocess.CalledProcessError:
            cmd = self._cmd_to_string(command)
            print('Failed to run command: {}'.format(cmd), file=sys.stderr)
            raise

    def check_west_call(self, args, **kwargs):
        '''Like check_call, but prepends the path to west as the command.'''
        # West makes its own assumptions about signal and exception
        # handling, hence the subprocess wrapper.
        #
        # The Windows launcher script west-win.py, when invoked
        # directly instead of through "py -3", is cross-platform.
        west = os.path.join(self.command_env['ZEPHYR_BASE'],
                            'scripts', 'west-win.py')
        self.check_call([sys.executable, west] + args, **kwargs)


#
# Build
#

class Build(Command):

    def __init__(self, *args, **kwargs):
        super(Build, self).__init__(*args, **kwargs)

    @property
    def command_name(self):
        return 'build'

    @property
    def command_help(self):
        return 'build application images'

    def do_register(self, parser):
        # Common arguments.
        parser.add_argument('-b', '--board', dest='boards', default=[],
                            action='append', help=HELP['--board'])
        parser.add_argument('-O', '--outdir', default=find_default_outdir(),
                            help=HELP['--outdir'])
        parser.add_argument('app', nargs='+', help=HELP['app'])
        parser.add_argument('-o', '--outputs', choices=BUILD_OUTPUTS + ['all'],
                            default='all',
                            help=HELP['--outputs'].format('build'))

        # Build-specific arguments
        parser.add_argument('-G', '--generator', default='Ninja',
                            help='''CMake generator to use; default is Ninja.
                            Note that you must run 'pristine' between builds
                            if you're switching build systems, or use different
                            build directories (e.g. with --outdir).''')
        parser.add_argument('-c', '--conf-file',
                            help='''If given, sets app (not mcuboot)
                                 configuration file(s)''')
        parser.add_argument('--oc', '--overlay-config', dest='overlay_config',
                            action='append', default=[],
                            help='''Additional config (.conf) file to overlay
                            onto the main application config files (which are
                            specified with --conf-file); may be given
                            multiple times to specify multiple files.''')
        parser.add_argument('-z', '--zephyr-toolchain-variant',
                            default=ZEPHYR_TOOLCHAIN_VARIANT_DEFAULT,
                            help='''Toolchain variant used by Zephyr
                                 (default: {})'''.format(
                                     ZEPHYR_TOOLCHAIN_VARIANT_DEFAULT))
        parser.add_argument('--prebuilt-toolchain', default='yes',
                            choices=['yes', 'no', 'y', 'n'],
                            help='''Whether to use a pre-built toolchain
                                 provided with ZMP, if one exists (default:
                                 'yes'). Currently, only a pre-built GNU ARM
                                 Embedded toolchain is provided. Set to 'no' to
                                 prevent overriding the toolchain's location in
                                 the calling environment.''')
        parser.add_argument('-j', '--jobs',
                            type=int, default=BUILD_PARALLEL_DEFAULT,
                            help='''Number of jobs to run simultaneously (the
                            default is derived from the number of available
                            CPUs)''')
        parser.add_argument('-K', '--signing-key',
                            help='''Path to signing key for application
                                 binary. WARNING: if not given, an INSECURE
                                 default key is used which should NOT be
                                 used for production images.''')
        parser.add_argument('-V', '--imgtool-version',
                            help='''Image version in X.Y.Z+B semantic
                                 versioning format (default: {})'''.format(
                                     MCUBOOT_IMGTOOL_VERSION_DEFAULT))
        parser.add_argument('--no-bootloader', '--skip-signature',
                            action='store_true',
                            help="""If set, don't build the application for
                                 chain-loading by MCUBoot. Use of this option
                                 implies -o app, and is incompatible with
                                 the -K and --imgtool-xxx options.""")
        parser.add_argument('--imgtool-pad', action='store_true',
                            help="""If given, the resulting signed image
                                 will include padding all the way out to the
                                 end of the sector. This is not normally a
                                 good idea, as it wastes space and consumes
                                 extra bandwidth to transmit.""")

    def do_prep_for_run(self):
        if self.arguments.no_bootloader:
            if self.arguments.signing_key is not None:
                raise ValueError('{} is incompatible with {}'.format(
                    '--no-bootloader', '--signing-key'))
            elif self.arguments.imgtool_version is not None:
                raise ValueError('{} is incompatible with {}'.format(
                    '--no-bootloader', '--imgtool-version'))
            elif self.arguments.imgtool_pad:
                raise ValueError('{} is incompatible with {}'.format(
                    '--no-bootloader', '--imgtool-pad'))
            self.arguments.outputs = 'app'
        else:
            if self.arguments.imgtool_version is None:
                default = MCUBOOT_IMGTOOL_VERSION_DEFAULT
                self.wrn('No --imgtool-version given, using {}'.format(
                    default))
                self.arguments.imgtool_version = default
            if not self.version_is_semver(self.arguments.imgtool_version):
                msg = '{} is not in semantic versioning format'
                raise ValueError(msg.format(self.arguments.imgtool_version))

            if self.arguments.signing_key is None:
                key = os.path.join(find_mcuboot_root(), MCUBOOT_DEV_KEY)
                self.insecure_requested = True
            else:
                key = self.arguments.signing_key
                self.insecure_requested = False
            self.arguments.signing_key = os.path.abspath(key)

        self.runner_core = importlib.import_module('.core', 'west.runner')

        check_boards(self.arguments.boards)
        check_dependencies(['cmake', 'dtc'])
        if self.arguments.generator == 'Ninja':
            check_dependencies(['ninja'])

    def do_invoke(self):
        for app in self.arguments.app:
            app = app.rstrip(os.path.sep)
            for board in self.arguments.boards:
                if 'mcuboot' in self.arguments.outputs:
                    self.build_mcuboot(app, board)
                if 'app' in self.arguments.outputs:
                    self.build_app(app, board)

    def cmake_build(self, sourcedir, outdir, gen_options):
        os.makedirs(outdir, exist_ok=True)

        if 'CMakeFiles' not in os.listdir(outdir):
            cmd_generate = (['cmake',
                             '-G{}'.format(self.arguments.generator)] +
                            CMAKE_OPTIONS +
                            gen_options + [shlex.quote(sourcedir)])
            self.check_call(cmd_generate, cwd=outdir)

        cmd_build = (['cmake',
                      '--build', shlex.quote(outdir),
                      '--',
                      '-j{}'.format(self.arguments.jobs)])
        self.check_call(cmd_build, cwd=outdir)

    def toolchain_args(self):
        if not self.arguments.prebuilt_toolchain.startswith('y'):
            return []

        toolchain_variant = self.arguments.zephyr_toolchain_variant
        if toolchain_variant == 'gnuarmemb':
            return [
                '-DZEPHYR_TOOLCHAIN_VARIANT=gnuarmemb',
                '-DGNUARMEMB_TOOLCHAIN_PATH={}'.format(
                    shlex.quote(find_arm_none_eabi_gcc()))]
        else:
            raise NotImplementedError(
                "no prebuilts available for {}".format(toolchain_variant))

    def build_mcuboot(self, app, board):
        outdir = find_mcuboot_outdir(self.arguments.outdir, app, board)
        mcuboot_source = os.path.join(find_mcuboot_root(), 'boot', 'zephyr')
        gen_options = ['-DBOARD={}'.format(board)] + self.toolchain_args()

        # If the application sources contain mcuboot.overlay, bring it
        # into the MCUboot build as well.
        app_source = find_app_root(app)
        mcuboot_overlay = os.path.join(app_source, 'mcuboot.overlay')
        if os.path.exists(mcuboot_overlay):
            gen_options.extend(['-DDTC_OVERLAY_FILE={}'.format(
                    shlex.quote(mcuboot_overlay))])

        # MCUboot requires a key Kconfig option, so we need an overlay
        # file; the only convenient ways to bake them in from here are
        # with an explicit -DOVERLAY_CONFIG=xx, or by putting the
        # setting into the build directory. Since we're generating it
        # dynamically, we choose the latter option to avoid messing
        # with the cmake command line.
        os.makedirs(outdir, exist_ok=True)
        key_overlay = os.path.join(outdir, 'mcuboot-key-file.conf')
        with open(key_overlay, 'w') as f:
            print('CONFIG_BOOT_SIGNATURE_KEY_FILE="{}"'.format(
                self.arguments.signing_key), file=f)

        self.cmake_build(mcuboot_source, outdir, gen_options)

    def build_app(self, app, board):
        outdir = find_app_outdir(self.arguments.outdir, app, board)
        gen_options = ['-DBOARD={}'.format(board)] + self.toolchain_args()
        overlay_config = self.arguments.overlay_config

        if self.arguments.conf_file:
            gen_options.append('-DCONF_FILE={}'.format(
                self.arguments.conf_file))

        if not self.arguments.no_bootloader:
            overlay_config.append(os.path.join(find_sdk_build_root(),
                                               'mcuboot-overlay.conf'))

        if overlay_config:
            gen_options.append('-DOVERLAY_CONFIG={}'.format(
                shlex.quote(';'.join(overlay_config))))

        self.cmake_build(find_app_root(app), outdir, gen_options)

        if not self.arguments.no_bootloader:
            self.sign_app(app, board)

    def sign_app(self, app, board):
        outdir = find_app_outdir(self.arguments.outdir, app, board)
        for cmd_sign in self.sign_commands(app, board, outdir):
            self.check_call(cmd_sign, cwd=outdir)
        if self.insecure_requested:
            self.wrn('Warning: used insecure default signing key.',
                     'IMAGES ARE NOT SUITABLE FOR PRODUCTION USE.')

    def sign_commands(self, app, board, outdir):
        ret = []

        bcfg = self.runner_core.BuildConfiguration(outdir)
        align = str(bcfg['FLASH_WRITE_BLOCK_SIZE'])
        vtoff = str(bcfg['CONFIG_TEXT_SECTION_OFFSET'])
        version = self.arguments.imgtool_version
        slot_size = str(bcfg['FLASH_AREA_IMAGE_0_SIZE'])

        # Always produce a signed binary.
        unsigned_bin = os.path.join(outdir, 'zephyr', 'zephyr.bin')
        signed_bin = signed_app_name(app, board, outdir, 'bin')
        ret.append(self.sign_command(align, vtoff, version, unsigned_bin,
                                     signed_bin, slot_size))

        # If there's a .hex file, sign that too. (Some Zephyr runners
        # can only flash hex files, e.g. the nrfjprog runner).
        unsigned_hex = os.path.join(outdir, 'zephyr', 'zephyr.hex')
        if os.path.isfile(unsigned_hex):
            signed_hex = signed_app_name(app, board, outdir, 'hex')
            ret.append(self.sign_command(align, vtoff, version, unsigned_hex,
                                         signed_hex, slot_size))

        return ret

    def sign_command(self, align, vtoff, version, infile, outfile, slot_size):
        cmd = ['/usr/bin/env', 'python3',
               os.path.join(find_mcuboot_root(), MCUBOOT_IMGTOOL),
               'sign',
               '--key', shlex.quote(self.arguments.signing_key),
               '--align', align,
               '--header-size', vtoff,
               '--slot-size', slot_size,
               '--version', shlex.quote(version),
               shlex.quote(infile),
               shlex.quote(outfile)]
        if self.arguments.imgtool_pad:
            cmd.append('--pad')

        return cmd

    def version_is_semver(self, version):
        return re.match('^\d+[.]\d+[.]\d+([+]\d+)?$', version) is not None


#
# Clean, Pristine
#

class CleanPristine:
    '''Mix-in class for clean and pristine target support'''

    def __init__(self, *args, **kwargs):
        target = kwargs.get('target')
        if target is None:
            raise ValueError('no target given')
        self.target = target
        del kwargs['target']
        super(CleanPristine, self).__init__(*args, **kwargs)

    @property
    def command_name(self):
        return self.target

    @property
    def command_help(self):
        return 'run build system {} target'.format(self.target)

    def do_register(self, parser):
        # Common arguments.
        parser.add_argument('-b', '--board', dest='boards', default=[],
                            action='append', help=HELP['--board'])
        parser.add_argument('-O', '--outdir', default=find_default_outdir(),
                            help=HELP['--outdir'])
        parser.add_argument('app', nargs='+', help=HELP['app'])
        parser.add_argument('-o', '--outputs', choices=BUILD_OUTPUTS + ['all'],
                            default='all',
                            help=HELP['--outputs'].format('build'))

    def do_prep_for_run(self):
        check_boards(self.arguments.boards)
        check_dependencies(['cmake'])

    def do_invoke(self):
        outdir = self.arguments.outdir
        for app in self.arguments.app:
            app = app.rstrip(os.path.sep)
            for board in self.arguments.boards:
                if 'mcuboot' in self.arguments.outputs:
                    outdir = find_mcuboot_outdir(outdir, app, board)
                    self.cmake_clean(outdir)
                if 'app' in self.arguments.outputs:
                    outdir = find_app_outdir(self.arguments.outdir, app, board)
                    self.cmake_clean(outdir)

    def cmake_clean(self, outdir):
        if not os.path.isdir(outdir):
            raise RuntimeError('build directory {} does not exist'.format(
                outdir))
        elif 'CMakeFiles' not in os.listdir(outdir):
            raise RuntimeError('no CMake files in {}; cannot run {}'.format(
                outdir, self.target))

        cmd_clean = (['cmake',
                      '--build', shlex.quote(outdir),
                      '--',
                      shlex.quote(self.target)])
        self.check_call(cmd_clean, cwd=outdir)


class Clean(CleanPristine, Command):

    def __init__(self, *args, **kwargs):
        kwargs['target'] = 'clean'
        super(Clean, self).__init__(*args, **kwargs)


class Pristine(CleanPristine, Command):

    def __init__(self, *args, **kwargs):
        kwargs['target'] = 'pristine'
        super(Pristine, self).__init__(*args, **kwargs)


#
# Configure
#

class Configure(Command):

    def __init__(self, *args, **kwargs):
        super(Configure, self).__init__(*args, **kwargs)

    @property
    def command_name(self):
        return 'configure'

    @property
    def command_help(self):
        return '''configure a build'''

    def do_register(self, parser):
        # Common:
        parser.add_argument('-b', '--board', dest='boards', default=[],
                            action='append', help=HELP['--board'])
        parser.add_argument('-O', '--outdir', default=find_default_outdir(),
                            help=HELP['--outdir'])
        parser.add_argument('-o', '--outputs', choices=BUILD_OUTPUTS + ['all'],
                            default='all',
                            help=HELP['--outputs'].format('configure'))
        parser.add_argument('app', help='application to configure')

        # Other:
        default = CONFIGURATOR_DEFAULT
        parser.add_argument(
            '-C', '--configurator',
            choices=CONFIGURATORS,
            default=default,
            help='''Configure front-end (default: {})'''.format(default))

    def do_invoke(self):
        mcuboot = find_mcuboot_root()

        for board in self.arguments.boards:
            app = self.arguments.app
            source_dirs = {'app': find_app_root(app), 'mcuboot': mcuboot}
            for output in self.arguments.outputs:
                self.do_configure(board, app, output, source_dirs[output])

    def do_configure(self, board, app, output, source_dir):
        if output == 'app':
            outdir = find_app_outdir(self.arguments.outdir, app, board)
        else:
            outdir = find_mcuboot_outdir(self.arguments.outdir, app, board)
        cmd_configure = ['cmake',
                         '--build', shlex.quote(outdir),
                         '--target', self.arguments.configurator]
        self.check_call(cmd_configure)


#
# Flash
#


class Flash(Command):

    def __init__(self, *args, **kwargs):
        super(Flash, self).__init__(*args, **kwargs)

    @property
    def command_name(self):
        return 'flash'

    @property
    def command_help(self):
        return 'flash a binary or binaries to a board'

    def do_register(self, parser):
        # Common:
        parser.add_argument('-b', '--board', dest='boards', default=[],
                            action='append', help=HELP['--board'])
        parser.add_argument('-O', '--outdir', default=find_default_outdir(),
                            help=HELP['--outdir'])
        parser.add_argument('-o', '--outputs', choices=BUILD_OUTPUTS + ['all'],
                            default='all',
                            help=HELP['--outputs'].format('flash'))
        parser.add_argument('app', help='application to flash')
        # Other:
        parser.add_argument('--board-id', dest='board_ids',
                            default=[], action='append',
                            help='''If given, specifies a --board-id
                            argument to the underlying flash runner''')

    def do_prep_for_run(self):
        if self.arguments.board_ids and len(self.arguments.boards) > 1:
            raise ValueError('only one board target may be used when '
                             'specifying --board-id')

        self.runner_core = importlib.import_module('.core', 'west.runner')
        self.arguments.app = self.arguments.app.strip(os.path.sep)

    def do_invoke(self):
        outdir = self.arguments.outdir
        app = self.arguments.app

        for board in self.arguments.boards:
            if self.arguments.board_ids:
                for board_id in self.arguments.board_ids:
                    self.west_flash(outdir, app, board, board_id=board_id)
            else:
                self.west_flash(outdir, app, board)

    def west_flash(self, outdir, app, board, board_id=None):
        app_outdir = find_app_outdir(outdir, app, board)
        bcfg = self.runner_core.BuildConfiguration(app_outdir)

        west_args = ['flash']
        if board_id is not None:
            west_args.extend(['--board-id', board_id])

        bootloader_mcuboot = bool(bcfg.get('CONFIG_BOOTLOADER_MCUBOOT'))
        if 'mcuboot' in self.arguments.outputs:
            if bootloader_mcuboot:
                mcuboot_outdir = find_mcuboot_outdir(outdir, app, board)
                args_extra = ['--build-dir', mcuboot_outdir]
                self.check_west_call(west_args + args_extra)
            else:
                msg = (
                    'Warning:\n'
                    '\tFlash of MCUboot requested, but the application is not compiled for MCUboot\n'  # noqa: E501
                    '\t. Ignoring request; not attempting MCUboot flash. You can run this command\n'  # noqa: E501
                    '\twith "-o app" to disable this message.')
                self.wrn(msg)

        if 'app' in self.arguments.outputs:
            args_extra = ['--build-dir', app_outdir]
            if bootloader_mcuboot:
                signed_bin = signed_app_name(app, board, app_outdir, 'bin')
                signed_hex = signed_app_name(app, board, app_outdir, 'hex')
                # Prefer hex to bin. (Some of the runners that take a hex don't
                # understand --dt-flash for a bin yet).
                if os.path.isfile(signed_hex):
                    args_extra.extend(['--kernel-hex', signed_hex])
                elif os.path.isfile(signed_bin):
                    args_extra.extend(['--dt-flash=y',
                                      '--kernel-bin', signed_bin])
            self.check_west_call(west_args + args_extra)
