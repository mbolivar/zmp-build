#!/usr/bin/env python3

'''Zephyr mergeup helper.

This is a helper script for dealing with upstream merges. It looks at
upstream changes that are not present in another tree, and automates
some of the bookkeeping related to creating mergeup commits for those
changes.
'''

import argparse
from collections import defaultdict
import os
import re
import sys

import pygit2

# This list maps the 'area' a commit affects to a list of
# shortlog prefixes (the content before the first ':') in the Zephyr
# commit shortlogs that belong to it.
#
# The values are lists of case-insensitive regular expressions that
# are matched against the shortlog prefix of each commit. Matches are
# done with regex.fullmatch().
#
# Keep its definition sorted alphabetically by key.
AREA_TO_SHORTLOG_RES = [
    ('Arches', ['arch', 'arc', 'arm', 'native', 'nios2', 'posix', 'lpc',
                'riscv32', 'soc', 'x86', 'xtensa']),
    ('Bluetooth', ['bluetooth']),
    ('Boards', ['boards?(/.*)?']),
    ('Build', ['build', 'cmake', 'kconfig', 'size_report',
               'gen_syscall_header', 'gen_isr_tables?', 'ld', 'linker']),
    ('Continuous Integration', ['ci', 'coverage', 'sanitycheck', 'gitlint']),
    ('Cryptography', ['crypto', 'mbedtls']),
    ('Documentation', ['docs?(/.*)?', 'CONTRIBUTING.rst', 'doxygen']),
    ('Device Tree', ['dts', 'dt-bindings', 'extract_dts_includes?']),
    ('Drivers', ['drivers?(/.*)?',
                 'adc', 'aio', 'clock_control', 'counter',
                 'crc', 'display', 'dma', 'entropy', 'eth', 'ethernet',
                 'flash', 'gpio', 'grove', 'i2c', 'i2s',
                 'interrupt_controller', 'ipm', 'led_strip', 'led', 'pci',
                 'pinmux', 'pwm', 'rtc', 'sensors?', 'serial', 'shared_irq',
                 'spi', 'timer', 'usb', 'watchdog',
                 # Technically in subsys/ (or parts are), but treated
                 # as drivers
                 'console', 'random', 'storage']),
    ('External', ['ext', 'hal', 'stm32cube']),
    ('File Systems', ['fs', 'disks?']),
    ('Firmware Update', ['dfu']),
    ('Kernel',  ['kernel(/.*)?', 'poll', 'mempool', 'syscalls']),
    ('Libraries', ['libc?', 'json', 'ring_buffer']),
    ('Maintainers', ['CODEOWNERS([.]rst)?']),
    ('Miscellaneous', ['misc', 'release', 'shell', 'printk', 'version']),
    ('Networking', ['net(/.*)?']),
    ('Samples', ['samples?']),
    ('Scripts', ['scripts?', 'runner']),
    ('Testing', ['tests?(/.*)?', 'testing', 'unittest', 'ztest']),
    ]


def _invert_keys_val_list(kvs):
    for k, vs in kvs:
        for v in vs:
            yield v, k


# This 'inverts' the key/value relationship in AREA_TO_SHORTLOG_RES to
# make a list from shortlog prefix REs to areas.
SHORTLOG_RE_TO_AREA = [(re.compile(k, flags=re.IGNORECASE), v) for k, v in
                       _invert_keys_val_list(AREA_TO_SHORTLOG_RES)]


AREAS = [a for a, _ in AREA_TO_SHORTLOG_RES]


class InvalidRepositoryError(RuntimeError):
    pass


def repo_mergeup_commits(repo_path, osf_ref, upstream_ref):
    if repo_path is None:
        repo_path = os.getcwd()

    try:
        repo = pygit2.Repository(repo_path)
    except KeyError:
        # pygit2 raises KeyError when the current path is not a Git
        # repository.
        msg = "Can't initialize Git repository at {}".format(repo_path)
        raise InvalidRepositoryError(msg)

    osf_oid = repo.revparse_single(osf_ref).oid
    upstream_oid = repo.revparse_single(upstream_ref).oid

    merge_base = repo.merge_base(osf_oid, upstream_oid)

    sort = pygit2.GIT_SORT_TOPOLOGICAL | pygit2.GIT_SORT_REVERSE
    walker = repo.walk(upstream_oid, sort)
    walker.hide(merge_base)

    return [c for c in walker]


def shortlog_is_revert(shortlog):
    return shortlog.startswith('Revert ')


def shortlog_reverts_what(shortlog):
    revert = 'Revert '
    return shortlog[len(revert):].strip('"')


def shortlog_area_prefix(shortlog):
    '''Get the prefix of a shortlog which describes its area.

    This returns the "raw" prefix as it appears in the shortlog. To
    canonicalize this to one of a known set of areas, use
    shortlog_area() instead. If no prefix is present, returns None.
    '''
    # Base case for recursion.
    if not shortlog:
        return None

    # 'Revert "foo"' should map to foo's area prefix.
    if shortlog_is_revert(shortlog):
        shortlog = shortlog_reverts_what(shortlog)
        return shortlog_area_prefix(shortlog)

    # If there is no ':', there is no area. Otherwise, the candidate
    # area is the substring up to the first ':'.
    if ':' not in shortlog:
        return None
    area, rest = [s.strip() for s in shortlog.split(':', 1)]

    # subsys: foo should map to foo's area prefix, etc.
    if area in ['subsys', 'include']:
        return shortlog_area_prefix(rest)

    return area


def shortlog_area(shortlog):
    '''Match a Zephyr commit shortlog to the affected area.

    If there is no match, returns None.'''
    area_pfx = shortlog_area_prefix(shortlog)

    if area_pfx is None:
        return None

    for test_regex, area in SHORTLOG_RE_TO_AREA:
        match = test_regex.fullmatch(area_pfx)
        if match:
            return area
    return None


def commit_shortsha(commit, len=8):
    '''Return a short version of the commit SHA.'''
    return str(commit.oid)[:len]


def commit_shortlog(commit):
    '''Return the first line in a commit's log message.'''
    return commit.message.splitlines()[0]


def commit_area(commit):
    '''From a Zephyr commit, get its area.'''
    return shortlog_area(commit_shortlog(commit))


def upstream_commit_line(commit):
    '''Get a line for a mergeup commit message about the given commit.'''
    return '- {} {}'.format(commit_shortsha(commit), commit_shortlog(commit))


def upstream_area_message(area, commits):
    '''Given an area and its commits, get mergeup commit text.'''
    return '\n'.join(
        ['{}:'.format(area),
         ''] +
        list(upstream_commit_line(c) for c in commits) +
        [''])


def dump_unknown_commit_help(unknown_commits):
    print("Error: can't build mergeup log message.",
          'The following commits have unknown areas:',
          file=sys.stderr)
    print(file=sys.stderr)
    for c in unknown_commits:
        print(upstream_commit_line(c), file=sys.stderr)
    print(file=sys.stderr)
    print('You can manually specify areas like so:', file=sys.stderr)
    print(file=sys.stderr)
    print(sys.argv[0], end='', file=sys.stderr)
    for c in unknown_commits:
        print(' --set-area {}:AREA'.format(commit_shortsha(c)),
              end='', file=sys.stderr)
    print(' ...', file=sys.stderr)
    print(file=sys.stderr)
    print('\n\t'.join(['Where each AREA is taken from the list:'] + AREAS))
    print(file=sys.stderr)
    print('You can also update AREA_TO_SHORTLOG_RES in {}'.format(
              sys.argv[0]),
          file=sys.stderr)
    print('to permanently associate an area with this type of shortlog.',
          file=sys.stderr)
    print(file=sys.stderr)


def check_known_area(commit, sha_to_area):
    sha = str(commit.oid)
    for k, v in sha_to_area.items():
        if sha.startswith(k):
            return v
    return None


def mergeup_highlights_changes(upstream_commits, sha_to_area):
    '''Create a mergeup commit log message template.

    Groups the iterable of upstream commits by area, dumping a message
    and exiting if any are unknown. Otherwise, returns a highlights
    template followed by the commit shortlogs grouped by area.

    The sha_to_area dict maps SHA prefixes to commit areas, and
    overrides the guesses otherwise made by this routine from the
    shortlog.
    '''
    area_commits = defaultdict(list)
    for c in upstream_commits:
        area = check_known_area(c, sha_to_area) or commit_area(c)
        area_commits[area].append(c)

    # Map each area to a message about it that should go in the
    # mergeup commit, keeping track of commits with unknown areas.
    unknown_area = None
    area_logs = dict()
    for area, commits in area_commits.items():
        if area is None:
            unknown_area = commits
            continue

        area_logs[area] = upstream_area_message(area, commits)

    # If any commits weren't be matched to an area, tell the user how
    # to specify them and exit.
    if unknown_area:
        dump_unknown_commit_help(unknown_area)
        sys.exit(1)

    # Create and return the mergeup commit log message template.
    message_lines = (
        ['Highlights',
         '==========',
         '',
         'Important Changes',
         '-----------------',
         '',
         '<Important changes, like API breaks, go here>',
         '',
         'Features',
         '--------',
         '',
         '<New features go here>',
         '',
         'Bug Fixes',
         '---------',
         '',
         '<Notable fixes or notes on large groups of fixes go here>',
         '',
         'Upstream Changes',
         '================',
         ''] +
        [area_logs[area] for area in sorted(area_logs)])

    return '\n'.join(message_lines)


def main(args):
    upstream_commits = repo_mergeup_commits(args.repo, args.osf_ref,
                                            args.upstream_ref)

    highlights_changes = mergeup_highlights_changes(upstream_commits,
                                                    args.sha_to_area)
    print(highlights_changes)


def _self_test():
    # Some areas, and shortlogs that should map to them
    area_shortlog_expected = [
        # Cases where we expect to match a shortlog to a particular area.

        ('Arches', 'ARM: stm32f0: fix syscfg mapping to fix EXTI config'),
        ('Arches', 'x86: mmu: kernel: Validate existing APIs'),
        ('Arches', 'arch: x86: fix jailhouse build'),
        ('Arches', 'arm: implement API to validate user buffer'),
        ('Bluetooth', 'Bluetooth: Mesh: Fix typo in Kconfig help message'),
        ('Boards', 'boards: nios2: altera_max10: cleanup board documentation'),
        ('Build', 'cmake: Fix the dependency between qemu and the elf file'),
        ('Build', 'kconfig: 802154: nrf: Fix kconfig'),
        ('Build', 'gen_syscall_header: create dummy handler refs'),
        ('Build', 'Revert "cmake: add zephyr_cc_option_nocheck"'),
        ('Build', 'gen_isr_tables: Minor refactoring'),
        ('Continuous Integration', 'sanitycheck: Flush stdout in info()'),
        ('Continuous Integration', 'ci: verify author identity'),
        ('Continuous Integration',
         'coverage: build with -O0 to get more information'),
        ('Continuous Integration',
         'gitlint: do not allow title-only commit messages'),
        ('Cryptography', 'crypto: Update TinyCrypt to 0.2.8'),
        ('Cryptography',
         'crypto: config: config-coap: ' +
         'add CONFIG for setting max content length'),
        ('Cryptography',
         'mbedtls: Kconfig: Re-organize to enable choosing an mbedtls impl.'),
        ('Device Tree',
         'include: dt-bindings: stm32_pinctrl: Add ports I, J, K'),
        ('Drivers', 'drivers: serial: stm32: report only unmasked irq'),
        ('Drivers', 'flash: stm32l4x: fix build'),
        ('Drivers', 'gpio: Introduce mcux igpio shim driver'),
        ('Drivers', 'clock_control: Introduce mcux ccm driver'),
        ('Drivers', 'serial: Add another instance to the mcux lpuart driver'),
        ('Drivers', 'drivers/ieee802154_kw41z: Fix interrupt priority'),
        ('Drivers',
         'usb: netusb: Use lower addresses for default endpoint config'),
        ('Documentation', 'doc/dts: Update to reflect new path locations'),
        ('Documentation', 'doc: boards: v2m_beetle: fix conversion to cmake'),
        ('Documentation', 'doxygen: ignore misc/util.h'),
        ('External', 'ext: hal: altera: Add Altera HAL README file'),
        ('Firmware Update',
         'dfu: replace FLASH_ALIGN with FLASH_WRITE_BLOCK_SIZE'),
        ('File Systems', 'disk: delete the GET_DISK_SIZE IOCTL.'),
        ('Kernel', 'kernel: stack: add -fstack-protector-all without checks'),
        ('Kernel',
         'Revert "kernel: stack: add -fstack-protector-all without checks"'),
        ('Kernel', 'poll: k_poll: Document -EINTR return'),
        ('Kernel', 'mempool: add assertion for calloc bounds overflow'),
        ('Kernel',
         'syscalls: REVERTME: clean up warnings when building unit tests'),
        ('Libraries',
         'libc: some architectures do not require baremetal libc'),
        ('Libraries', 'lib: move ring_buffer from misc/ to lib/'),
        ('Libraries', 'ring_buffer: remove broken object_tracing support'),
        ('Maintainers', 'CODEOWNERS: misc updates'),
        ('Maintainers', 'CODEOWNERS.rst: misc updates'),
        ('Miscellaneous',
         'printk: Add padding support to string format specifiers'),
        ('Miscellaneous',
         'version: fix version handling without extra_version set'),
        ('Miscellaneous', 'misc: Use braces in infinite for loop'),
        ('Networking', 'net: if: fix ND reachable calculation'),
        ('Networking', 'net/ieee802154: Make RAW mode generic'),
        ('Samples', 'samples: echo_server: Test the nrf build in CI'),
        ('Scripts',
         'scripts: runner: nrfjprog: remove BOARD environment requirement'),
        ('Scripts', "scripts: jlink: Don't reset after load"),
        ('Scripts', 'runner: nrfjprog: Improve error messages'),
        ('Testing', 'tests: use cmake to build object benchmarks'),
        ('Testing',
         'tests: mem_pool: ' +
         'Fixed memory pool test case failure on quark d2000.'),
        ('Testing',
         'tests/kernel/mem_protect/userspace: ' +
         'test that _k_neg_eagain is in rodata'),
        ('Testing', 'unittest: Support EXTRA_*_FLAGS'),
        ('Testing', 'testing: add option to generate coverage reports'),

        # Cases we explicitly do not want to match, and why:

        # Tree-wide change with no particular area.
        (None, 'Introduce cmake-based rewrite of KBuild'),

        # Should have been 'boards: mimxrt1050_evk' or so.
        (None, 'mimxrt1050_evk'),

        # Should have been 'arm: _setup_new_thread' or so.
        (None, '_setup_new_thread: fix crash on ARM'),
    ]

    for expected, shortlog in area_shortlog_expected:
        print('shortlog:', shortlog)
        actual = shortlog_area(shortlog)
        assert actual == expected, \
            'shortlog: {}, expected: {}, actual: {}'.format(
                shortlog, expected, actual)
        print('    area:', expected)

    print('OK')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='''Zephyr mergeup helper
                                     script. This script currently just
                                     prints the mergeup commit message.''')
    parser.add_argument('--areas', action='store_true',
                        help='''Print all areas that upstream commits are
                        grouped into in mergeup commit logs, and exit.''')
    parser.add_argument('--osf-ref', default='osf-dev/master',
                        help='''OSF ref (commit-ish) to compute a mergeup into.
                        Default is osf-dev/master.''')
    parser.add_argument('--upstream-ref', default='upstream/master',
                        help='''Upstream ref (commit-ish) to compute a mergeup
                        from.  Default is upstream/master.''')
    parser.add_argument('-A', '--set-area', default=[], action='append',
                        help='''Format is sha:Area; associates an area with
                        a commit SHA. Use --areas to print all areas.''')
    parser.add_argument('--self-test', action='store_true',
                        help='Perform an internal self-test, and exit.')
    parser.add_argument('repo', nargs='?',
                        help='''Path to the zephyr repository. If not given,
                        the current working directory is assumed.''')
    args = parser.parse_args()

    if args.self_test:
        _self_test()
        sys.exit(0)
    if args.areas:
        print('\n'.join(AREAS))
        sys.exit(0)

    sha_to_area = dict()
    for sha_area in args.set_area:
        sha, area = sha_area.split(':')
        if area not in AREAS:
            print('Invalid area {} for commit {}.'.format(area, sha),
                  file=sys.stderr)
            print('Choices:', ', '.join(AREAS), file=sys.stderr)
            sys.exit(1)
        sha_to_area[sha] = area
    args.sha_to_area = sha_to_area

    main(args)
