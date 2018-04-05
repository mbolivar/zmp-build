#!/usr/bin/env python3

# Copyright 2018 Open Source Foundries, Limited

'''Zephyr "what's new"? script.

This is a helper script for understanding what's happened in Zephyr
since a particular point in time. It looks at changes in an "upstream"
Zephyr tree that are not present in an OSF tree, and outputs
information on the differences between them.

This information is useful for general understanding, for creating OSF
mergeup commit messages, etc.
'''

import argparse
from collections import defaultdict, OrderedDict
import os
import re
from subprocess import check_output
import sys

import pygit2
import editdistance

from pygit2_helpers import shortlog_is_revert, shortlog_reverts_what, \
    shortlog_no_sauce, commit_shortsha, commit_shortlog, \
    commit_is_osf, upstream_commit_line

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
    ('Arches', ['arch(/.*)?', 'arc(/.*)?', 'arm(/.*)?', 'esp32(/.*)?',
                'native(/.*)?', 'nios2(/.*)?', 'posix(/.*)?', 'lpc(/.*)?',
                'riscv32(/.*)?', 'soc(/.*)?', 'x86(/.*)?', 'xtensa(/.*)?']),
    ('Bluetooth', ['bluetooth']),
    ('Boards', ['boards?(/.*)?']),
    ('Build', ['build', 'cmake', 'kconfig', 'size_report',
               'gen_syscall_header', 'gen_isr_tables?', 'ld', 'linker',
               'toolchain']),
    ('Continuous Integration', ['ci', 'coverage', 'sanitycheck', 'gitlint']),
    ('Cryptography', ['crypto', 'mbedtls']),
    ('Documentation', ['docs?(/.*)?', 'CONTRIBUTING.rst', 'doxygen']),
    ('Device Tree', ['dts(/.*)?', 'dt-bindings', 'extract_dts_includes?']),
    ('Drivers', ['drivers?(/.*)?',
                 'adc', 'aio', 'clock_control', 'counter', 'crc',
                 'device([.]h)?', 'display', 'dma', 'entropy', 'eth',
                 'ethernet',
                 'flash', 'gpio', 'grove', 'i2c', 'i2s',
                 'interrupt_controller', 'ipm', 'led_strip', 'led', 'pci',
                 'pinmux', 'pwm', 'rtc', 'sensors?', 'serial', 'shared_irq',
                 'spi', 'timer', 'uart', 'usb', 'watchdog',
                 # Technically in subsys/ (or parts are), but treated
                 # as drivers
                 'console', 'random', 'storage']),
    ('External', ['ext(/.*)?', 'hal', 'stm32cube']),
    ('Storage', ['fs', 'disks?', 'fcb']),
    ('Firmware Update', ['dfu', 'mgmt']),
    ('Kernel',  ['kernel(/.*)?', 'poll', 'mempool', 'syscalls', 'work_q',
                 'init.h']),
    ('Libraries', ['libc?', 'json', 'ring_buffer']),
    ('Maintainers', ['CODEOWNERS([.]rst)?']),
    ('Miscellaneous', ['misc', 'release', 'shell', 'printk', 'version']),
    ('Networking', ['net(/.*)?', 'openthread', 'slip']),
    ('Samples', ['samples?(/.*)?']),
    ('Scripts', ['scripts?(/.*)?', 'runner', 'gen_syscalls.py',
                 'gen_syscall_header.py']),
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


def repo_osf_commits(repo_path, osf_ref, upstream_ref):
    '''Commits reachable in repo_path from osf_ref, but not upstream_ref.'''
    # Note: pygit2 doesn't seem to have any ready-made rev-list
    # equivalent, so call out to git directly to get the commit SHAs,
    # then wrap them with pygit2 objects.
    #
    # TODO: reimplement in pure pygit2.
    if repo_path is None:
        repo_path = os.getcwd()

    try:
        repo = pygit2.Repository(repo_path)
    except KeyError:
        # pygit2 raises KeyError when the current path is not a Git
        # repository.
        msg = "Can't initialize Git repository at {}".format(repo_path)
        raise InvalidRepositoryError(msg)

    cmd = ['git', 'rev-list', '--pretty=oneline', '--reverse',
           osf_ref, '^{}'.format(upstream_ref)]
    output_raw = check_output(cmd, cwd=repo_path)
    output = output_raw.decode(sys.getdefaultencoding()).splitlines()

    ret = []
    for line in output:
        sha, _ = line.split(' ', 1)
        commit = repo.revparse_single(sha)
        ret.append(commit)

    return ret


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


def commit_area(commit):
    '''From a Zephyr commit, get its area.'''
    return shortlog_area(commit_shortlog(commit))


def upstream_area_message(area, commits):
    '''Given an area and its commits, get mergeup commit text.'''
    return '\n'.join(
        ['{} ({}):'.format(area, len(commits)),
         ''] +
        list(upstream_commit_line(c) for c in commits) +
        [''])


def areas_summary(area_commits):
    '''Get mergeup commit text summary for all areas.'''
    def area_patch_str_len(area):
        return len(str(area_commits[area]))
    areas_sorted = sorted(area_commits)

    pad = 4
    area_fill = len(max(area_commits, key=len)) + pad
    patch_fill = len(max(area_commits, key=area_patch_str_len))

    ret = [
        'Area summary ({} patches total):'.format(
            sum(len(v) for _, v in area_commits.items())),
        '',
        '{} Patches'.format('Area'.ljust(area_fill)),
        '{} -------'.format('-' * (area_fill - pad) + ' ' * pad),
    ]
    for area in areas_sorted:
        patches = area_commits[area]
        ret.append('{} {}'.format(
            area.ljust(area_fill),
            str(len(patches)).rjust(patch_fill)))
    ret.append('')

    return ret


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
        areas_summary(area_commits) +
        [area_logs[area] for area in sorted(area_logs)])

    return '\n'.join(message_lines)


def mergeup_outstanding_merged(osf_commits, upstream_commits,
                               edit_dist_threshold=3):
    '''Compute outstanding and likely merged osf_commits.

    Outstanding patches are commits in osf_commits which do not have a
    corresponding 'Revert "[OSF xxx] something something' later in the
    series.

    Likely merged patches are outstanding patches whose shortlog
    messages (with the OSF sauce tag removed) are within a given edit
    distance from something present in upstream_commits.

    The osf_commits list should reflect *all* patches in the OSF tree
    that aren't present upstream, not just the ones since the most
    recent merge base with upstream. By contrast, upstream_commits
    should be *just* the upstream patches that are involved in the
    mergeup.

    A (outstanding, likely_merged) is returned. The first member,
    outstanding, is an OrderedDict mapping shortlogs (with OSF sauce
    tag) to commit objects. The second member, likely_merged, is an
    OrderedDict mapping shortlogs (with OSF sauce tags) that are
    likely merged upstream (by edit distance threshold) to a list of
    the upstream commits that are the likely upstream merge commit.
    '''
    # Compute outstanding patches.
    outstanding = OrderedDict()
    for c in osf_commits:
        if len(c.parents) > 1:
            # Skip all the mergeup commits.
            continue

        sl = commit_shortlog(c)

        if shortlog_is_revert(sl):
            # If a shortlog marks a revert, delete the original commit
            # from outstanding.
            what = shortlog_reverts_what(sl)
            if what not in outstanding:
                import pprint
                pprint.pprint(outstanding)

                msg = "{} was reverted, but isn't present in OSF history"
                raise RuntimeError(msg.format(what))
            del outstanding[what]
        else:
            # Non-revert commits just get appended onto
            # outstanding, keyed by shortlog to make finding
            # them later in case they're reverted easier.
            #
            # We could try to support this by looking into the entire
            # revert message to find the "This reverts commit SHA"
            # text and computing reverts based on oid rather than
            # shortlog. That'd be more robust, but let's not worry
            # about it for now.
            if sl in outstanding:
                msg = 'duplicated commit shortlogs ({})'.format(sl)
                raise NotImplementedError(msg)
            outstanding[sl] = c

    # Compute likely merged patches.
    upstream_osf = [c for c in upstream_commits if commit_is_osf(c)]
    likely_merged = OrderedDict()
    for osf_sl, osf_c in outstanding.items():
        def ed(upstream_commit):
            return editdistance.eval(shortlog_no_sauce(osf_sl),
                                     commit_shortlog(upstream_commit))
        matches = [c for c in upstream_osf if ed(c) < edit_dist_threshold]
        if len(matches) != 0:
            likely_merged[osf_sl] = matches

    return outstanding, likely_merged


def mergeup_outstanding_summary(osf_only, upstream_commits):
    outstanding, likely_merged = mergeup_outstanding_merged(osf_only,
                                                            upstream_commits)
    ret = []

    def addl(line, comment=False):
        if comment:
            if line:
                ret.append('# {}'.format(line))
            else:
                ret.append('#')
        else:
            ret.append(line)

    addl('Outstanding OSF patches')
    addl('=======================')
    addl('')
    for sl, c in outstanding.items():
        addl('- {} {}'.format(commit_shortsha(c), sl))
    addl('')

    if not likely_merged:
        return '\n'.join(ret)

    addl('Likely merged OSF patches:', True)
    addl('IMPORTANT: You probably need to revert these and re-run!', True)
    addl('           Make sure to check the above as well; these are', True)
    addl("           guesses that aren't always right.", True)
    addl('', True)
    for sl, commits in likely_merged.items():
        addl('- "{}", likely merged as one of:'.format(sl), True)
        for c in commits:
            addl('\t- {} {}'.format(commit_shortsha(c),
                                    commit_shortlog(c)),
                 True)
        addl('', True)

    return '\n'.join(ret)


def main(args):
    # Upstream commits since the last mergeup.
    upstream_commits = repo_mergeup_commits(args.repo, args.osf_ref,
                                            args.upstream_ref)
    # OSF commits SINCE ORIGINAL BASELINE COMMIT WITH UPSTREAM.
    osf_only = repo_osf_commits(args.repo, args.osf_ref, args.upstream_ref)

    highlights_changes = mergeup_highlights_changes(upstream_commits,
                                                    args.sha_to_area)
    outstanding_summary = mergeup_outstanding_summary(osf_only,
                                                      upstream_commits)
    print(highlights_changes)
    print(outstanding_summary)


def _self_test():
    # Some areas, and shortlogs that should map to them
    area_shortlog_expected = [
        # Cases where we expect to match a shortlog to a particular area.

        ('Arches', 'ARM: stm32f0: fix syscfg mapping to fix EXTI config'),
        ('Arches', 'x86: mmu: kernel: Validate existing APIs'),
        ('Arches', 'arch: x86: fix jailhouse build'),
        ('Arches', 'arm: implement API to validate user buffer'),
        ('Arches',
         'xtensa/asm2: Add a _new_thread implementation for asm2/switch'),
        ('Arches', 'esp32: Set CPU pointer on app cpu at startup'),
        ('Bluetooth', 'Bluetooth: Mesh: Fix typo in Kconfig help message'),
        ('Boards', 'boards: nios2: altera_max10: cleanup board documentation'),
        ('Build', 'cmake: Fix the dependency between qemu and the elf file'),
        ('Build', 'kconfig: 802154: nrf: Fix kconfig'),
        ('Build', 'gen_syscall_header: create dummy handler refs'),
        ('Build', 'Revert "cmake: add zephyr_cc_option_nocheck"'),
        ('Build', 'gen_isr_tables: Minor refactoring'),
        ('Build', 'toolchain: organise toolchain/compiler files'),
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
        ('Device Tree', 'dts/arm: Move i2c2 node inside stm32fxxx dtsi file'),
        ('Device Tree', 'dts/arm/st: fix dts inclusion for stm32f334'),
        ('Drivers', 'drivers: serial: stm32: report only unmasked irq'),
        ('Drivers', 'flash: stm32l4x: fix build'),
        ('Drivers', 'gpio: Introduce mcux igpio shim driver'),
        ('Drivers', 'clock_control: Introduce mcux ccm driver'),
        ('Drivers', 'serial: Add another instance to the mcux lpuart driver'),
        ('Drivers', 'drivers/ieee802154_kw41z: Fix interrupt priority'),
        ('Drivers',
         'usb: netusb: Use lower addresses for default endpoint config'),
        ('Drivers', 'device: cleanup header layout'),
        ('Drivers', 'uart: fixing pin range being too tight for the nrf52840'),
        ('Drivers',
         'device.h: doc: Refactor to keep documentation infront of impl.'),
        ('Documentation', 'doc/dts: Update to reflect new path locations'),
        ('Documentation', 'doc: boards: v2m_beetle: fix conversion to cmake'),
        ('Documentation', 'doxygen: ignore misc/util.h'),
        ('External', 'ext: hal: altera: Add Altera HAL README file'),
        ('External', 'ext/hal: stm32cube: Update STM32F0 README file'),
        ('Firmware Update',
         'dfu: replace FLASH_ALIGN with FLASH_WRITE_BLOCK_SIZE'),
        ('Firmware Update', 'subsys: mgmt: SMP protocol for mcumgr.'),
        ('Storage', 'disk: delete the GET_DISK_SIZE IOCTL.'),
        ('Storage',
         'subsys: fcb: Check for mutex lock failure when walking FCB'),
        ('Kernel', 'kernel: stack: add -fstack-protector-all without checks'),
        ('Kernel',
         'Revert "kernel: stack: add -fstack-protector-all without checks"'),
        ('Kernel', 'poll: k_poll: Document -EINTR return'),
        ('Kernel', 'mempool: add assertion for calloc bounds overflow'),
        ('Kernel',
         'syscalls: REVERTME: clean up warnings when building unit tests'),
        ('Kernel',
         ('work_q: Correctly clear pending flag in delayed work queue, '
          'update docs')),
        ('Kernel', 'init.h: Fix english in comment'),
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
        ('Networking', 'openthread: Use ccache when enabled'),
        ('Networking', 'slip: fix a bug when in non-TAP mode.'),
        ('Samples', 'samples: echo_server: Test the nrf build in CI'),
        ('Samples',
         'samples/xtensa-asm2: Unit test for new Xtensa assembly primitives'),
        ('Scripts',
         'scripts: runner: nrfjprog: remove BOARD environment requirement'),
        ('Scripts', "scripts: jlink: Don't reset after load"),
        ('Scripts', 'runner: nrfjprog: Improve error messages'),
        ('Scripts',
         'scripts/dts: '
         'Use 4-spaces tabs instead of 2-space tabs in devicetree.py'),
        ('Scripts',
         'script/dts: Remove unnecessary empty return on functions'),
        ('Scripts', 'gen_syscalls.py: fix include issue'),
        ('Scripts', 'gen_syscall_header.py: fix include issue'),
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
