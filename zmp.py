#!/usr/bin/env python3

# Copyright (c) 2017 Linaro Limited.
# Copyright (c) 2017 Open Source Foundries Limited.
#
# SPDX-License-Identifier: Apache-2.0

import argparse
import os
import subprocess
import sys

# Ensure west is importable from commands.
WEST_SRC = os.path.join(
    subprocess.check_output('repo list -fpr west'.split()).decode(
        sys.getdefaultencoding()).strip(),
    'src')
sys.path.append(WEST_SRC)

from commands import Command

PROGRAM = sys.argv[0]
ARGV = sys.argv[1:]


def main():
    # Parsing is split into a multilevel structure based on the top-level
    # command. The first level is $scriptname [-h] $command [command_arg ...]
    top_parser = argparse.ArgumentParser()
    top_parser.add_argument('--debug', default=False, action='store_true',
                            help='If set, print extra debugging information.')
    cmd_parsers = top_parser.add_subparsers(help='command', dest='cmd')

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

    command_handlers[args.cmd].invoke(args)


if __name__ == '__main__':
    main()
