# Copyright (c) 2017 Linaro Limited.
# Copyright (c) 2017 Open Source Foundries Limited.
#
# SPDX-License-Identifier: Apache-2.0

import glob
import os
import platform
import shutil
import sys


# We could be smarter about this (search for .repo, e.g.), but it seems
# unnecessary.
ZMP_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

# Checked out paths for important repositories relative to ZMP root.
# TODO: parse these from the repo manifest, for robustness, at some point.
ZEPHYR_PATH = 'zephyr'

# The name of the directory which is the default root of the build hierarchy,
# relative to the .repo root.
BUILD_DIR_DEFAULT = 'outdir'

# Where mcuboot is relative to the .repo top level.
MCUBOOT_PATH = 'mcuboot'


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


def find_default_outdir():
    '''Get absolute path of default output directory.'''
    return os.path.join(find_zmp_root(), BUILD_DIR_DEFAULT)


def find_app_outdir(outdir, app, board, output):
    '''Get output (build) directory for an app output.'''
    return os.path.join(outdir, app, board, output)


def find_zephyr_board_dir(board_name):
    '''Get directory containing Zephyr board definition, or None.'''
    results = glob.glob(os.path.join(find_zephyr_base(),
                                     'boards', '*', board_name))
    if len(results) == 1:
        return results[0]
    return None


def check_boards(board_names, stream=sys.stderr):
    '''Check for Zephyr boards.

    If all the boards can be found, returns without error. Otherwise,
    prints an error and raises FileNotFoundError.'''
    not_found = [b for b in board_names if find_zephyr_board_dir(b) is None]
    if not_found:
        msg = 'Unknown board{}: {}'.format('s' if len(not_found) > 1 else '',
                                           ', '.join(not_found))
        print(msg, file=stream)
        raise FileNotFoundError(msg)


def check_dependencies(programs, stream=sys.stderr):
    '''Check for binary program dependencies.

    If all the programs in the argument iterable can be found, returns
    without error.  Otherwise, prints an error and raises
    FileNotFoundError.'''
    not_found = [p for p in programs if shutil.which(p) is None]
    if not_found:
        msg = 'Missing dependenc{}: {}'.format(
            'ies' if len(not_found) > 1 else 'y',
            ', '.join(not_found))
        print(msg, file=stream)
        raise FileNotFoundError(msg)
