# Copyright (c) 2017 Linaro Limited.
# Copyright (c) 2017 Open Source Foundries Limited.
#
# SPDX-License-Identifier: Apache-2.0

import os
import platform


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
