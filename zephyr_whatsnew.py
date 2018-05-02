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

import abc
import argparse
from collections import defaultdict, OrderedDict, namedtuple
import os
import re
from subprocess import check_output
import sys

import pygit2
import editdistance

from pygit2_helpers import shortlog_is_revert, shortlog_reverts_what, \
    shortlog_no_sauce, commit_shortsha, commit_shortlog, \
    commit_is_osf

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
                 'flash', 'gpio', 'grove', 'hid', 'i2c', 'i2s',
                 'interrupt_controller', 'ipm', 'led_strip', 'led', 'netusb',
                 'pci', 'pinmux', 'pwm', 'rtc', 'sensors?(/.*)?', 'serial',
                 'shared_irq', 'spi', 'timer', 'uart', 'uart_pipe', 'usb',
                 'watchdog',
                 # Technically in subsys/ (or parts are), but treated
                 # as drivers
                 'console', 'random', 'storage']),
    ('External', ['ext(/.*)?', 'hal', 'stm32cube']),
    ('Firmware Update', ['dfu', 'mgmt']),
    ('Kernel',  ['kernel(/.*)?', 'poll', 'mempool', 'syscalls', 'work_q',
                 'init.h']),
    ('Libraries', ['libc?', 'json', 'ring_buffer']),
    ('Maintainers', ['CODEOWNERS([.]rst)?']),
    ('Miscellaneous', ['misc', 'release', 'shell', 'printk', 'version']),
    ('Networking', ['net(/.*)?', 'openthread', 'slip']),
    ('Samples', ['samples?(/.*)?']),
    ('Scripts', ['scripts?(/.*)?', 'runner', 'gen_syscalls.py',
                 'gen_syscall_header.py', 'kconfiglib']),
    ('Storage', ['fs', 'disks?', 'fcb']),
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


#
# Repository analysis
#

class InvalidRepositoryError(RuntimeError):
    pass


class UnknownCommitsError(RuntimeError):
    '''Commits with unknown areas are present.

    The exception arguments are an iterable of commits whose area
    was unknown.
    '''
    pass


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


# ZephyrRepoAnalysis: represents results of analyzing Zephyr and OSF
# activity in a repository from given starting points. See
# ZephyrRepoAnalyzer.
#
# - upstream_area_counts: map from areas to total number of
#   new upstream patches (new means not reachable from `osf_ref`)
#
# - upstream_area_patches: map from areas to chronological (most
#   recent first) list of new upstream patches
#
# - osf_outstanding_patches: chronological list of OSF patches that don't
#   appear to have been reverted yet.
#
# - osf_merged_patches: "likely merged" OSF patches; a map from
#   shortlogs of unreverted OSF patches to lists of new upstream
#   patches sent by OSF contributors that have similar shortlogs.
ZephyrRepoAnalysis = namedtuple('ZephyrRepoAnalysis',
                                ['upstream_area_counts',
                                 'upstream_area_patches',
                                 'osf_outstanding_patches',
                                 'osf_merged_patches'])


class ZephyrRepoAnalyzer:
    '''Utility class for analyzing a Zephyr repository.'''

    def __init__(self, repo_path, osf_ref, upstream_ref, sha_to_area=None,
                 edit_dist_threshold=3):
        if sha_to_area is None:
            sha_to_area = {}

        self.sha_to_area = sha_to_area
        '''map from Zephyr SHAs to known areas, when they can't be guessed'''

        self.repo_path = repo_path
        '''path to Zephyr repository being analyzed'''

        self.osf_ref = osf_ref
        '''ref (commit-ish) for OSF commit to start analysis from'''

        self.upstream_ref = upstream_ref
        '''ref (commit-ish) for upstream ref to start analysis from'''

        self.edit_dist_threshold = edit_dist_threshold
        '''commit shortlog edit distance to match up OSF patches'''

    def analyze(self):
        '''Analyze repository history.

        If this returns without raising an exception, the return value
        is a ZephyrRepoAnalysis.
        '''
        try:
            self.repo = pygit2.Repository(self.repo_path)
        except KeyError:
            # pygit2 raises KeyError when the current path is not a Git
            # repository.
            msg = "Can't initialize Git repository at {}"
            raise InvalidRepositoryError(msg.format(self.repo_path))

        #
        # Group all upstream commits by area, and collect patch counts.
        #
        upstream_new = self._new_upstream_only_commits()
        upstream_area_patches = defaultdict(list)
        for c in upstream_new:
            area = self._check_known_area(c) or commit_area(c)
            upstream_area_patches[area].append(c)

        unknown_area = upstream_area_patches.get(None)
        if unknown_area:
            raise UnknownCommitsError(*unknown_area)

        upstream_area_counts = {}
        for area, patches in upstream_area_patches.items():
            upstream_area_counts[area] = len(patches)

        #
        # Analyze OSF portion of the tree.
        #
        osf_only = self._all_osf_only_commits()
        osf_outstanding = OrderedDict()
        for c in osf_only:
            if len(c.parents) > 1:
                # Skip all the mergeup commits.
                continue

            sl = commit_shortlog(c)

            if shortlog_is_revert(sl):
                # If a shortlog marks a revert, delete the original commit
                # from outstanding.
                what = shortlog_reverts_what(sl)
                if what not in osf_outstanding:
                    msg = "{} was reverted, but isn't present in OSF history"
                    raise RuntimeError(msg.format(what))
                del osf_outstanding[what]
            else:
                # Non-revert commits just get appended onto
                # osf_outstanding, keyed by shortlog to make finding
                # them later in case they're reverted easier.
                #
                # We could try to support this by looking into the entire
                # revert message to find the "This reverts commit SHA"
                # text and computing reverts based on oid rather than
                # shortlog. That'd be more robust, but let's not worry
                # about it for now.
                if sl in osf_outstanding:
                    msg = 'duplicated commit shortlogs ({})'.format(sl)
                    raise NotImplementedError(msg)
                osf_outstanding[sl] = c

        # Compute likely merged patches.
        upstream_osf = [c for c in upstream_new if commit_is_osf(c)]
        likely_merged = OrderedDict()
        for osf_sl, osf_c in osf_outstanding.items():
            def ed(upstream_commit):
                return editdistance.eval(shortlog_no_sauce(osf_sl),
                                         commit_shortlog(upstream_commit))
            matches = [c for c in upstream_osf if
                       ed(c) < self.edit_dist_threshold]
            if len(matches) != 0:
                likely_merged[osf_sl] = matches

        return ZephyrRepoAnalysis(upstream_area_counts,
                                  upstream_area_patches,
                                  osf_outstanding,
                                  likely_merged)

    def _new_upstream_only_commits(self):
        '''Commits in `upstream_ref` history since merge base with `osf_ref`'''
        osf_oid = self.repo.revparse_single(self.osf_ref).oid
        upstream_oid = self.repo.revparse_single(self.upstream_ref).oid

        merge_base = self.repo.merge_base(osf_oid, upstream_oid)

        sort = pygit2.GIT_SORT_TOPOLOGICAL | pygit2.GIT_SORT_REVERSE
        walker = self.repo.walk(upstream_oid, sort)
        walker.hide(merge_base)

        return [c for c in walker]

    def _check_known_area(self, commit):
        sha = str(commit.oid)
        for k, v in self.sha_to_area.items():
            if sha.startswith(k):
                return v
        return None

    def _all_osf_only_commits(self):
        '''Commits reachable from `osf_ref`, but not `upstream_ref`'''
        # Note: pygit2 doesn't seem to have any ready-made rev-list
        # equivalent, so call out to git directly to get the commit SHAs,
        # then wrap them with pygit2 objects.
        cmd = ['git', 'rev-list', '--pretty=oneline', '--reverse',
               self.osf_ref, '^{}'.format(self.upstream_ref)]
        output_raw = check_output(cmd, cwd=self.repo_path)
        output = output_raw.decode(sys.getdefaultencoding()).splitlines()

        ret = []
        for line in output:
            sha, _ = line.split(' ', 1)
            commit = self.repo.revparse_single(sha)
            ret.append(commit)

        return ret


#
# Output formatting
#

class ZephyrOutputFormatter(abc.ABC):
    '''Abstract base class for output formatters.'''

    @classmethod
    @abc.abstractmethod
    def names(cls):
        '''Name(s) of the output format'''

    @classmethod
    def get_by_name(cls, name):
        '''Get an output formatter class by format name.'''
        for sub_cls in ZephyrOutputFormatter.__subclasses__():
            names = sub_cls.names()
            if isinstance(names, str):
                if name == names:
                    return sub_cls
            else:
                if name in names:
                    return sub_cls
        raise ValueError('no output formatter for {}'.format(name))

    @abc.abstractmethod
    def get_output(self, repo_analysis):
        '''Get formatted output from a repo analysis.

        For now, this must be print()able.'''


class ZephyrTextFormatMixin:
    '''Plain text output formatter mix-in class.
    '''

    def do_get_output(self, analysis, include_osf_outstanding=True):
        '''Convenient hook for subclasses to use.'''
        highlights = self._highlights(analysis)
        individual_changes = self._individual_changes(analysis)
        if include_osf_outstanding:
            osf_outstanding = self._osf_outstanding(analysis)
        else:
            osf_outstanding = []
        return '\n'.join(highlights + individual_changes + osf_outstanding)

    def upstream_commit_line(self, commit):
        '''Get a line about the given upstream commit.'''
        return '- {} {}'.format(commit_shortsha(commit),
                                commit_shortlog(commit))

    def _highlights(self, analysis):
        '''Create a mergeup commit log message template.

        Groups the iterable of upstream commits by area, dumping a message
        and exiting if any are unknown. Otherwise, returns a highlights
        template followed by the commit shortlogs grouped by area.

        The sha_to_area dict maps SHA prefixes to commit areas, and
        overrides the guesses otherwise made by this routine from the
        shortlog.
        '''
        return [
            'Highlights',
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
            '']

    def _upstream_area_message(self, area, commits):
        '''Given an area and its commits, get mergeup commit text.'''
        return '\n'.join(
            ['{} ({}):'.format(area, len(commits)),
             ''] +
            list(self.upstream_commit_line(c) for c in commits) +
            [''])

    def _areas_summary(self, analysis):
        '''Get mergeup commit text summary for all areas.'''
        area_counts = analysis.upstream_area_counts
        total = sum(area_counts.values())

        def area_count_str_len(area):
            count = area_counts[area]
            return len(str(count))
        areas_sorted = sorted(area_counts)

        ret = [
            'Patches by area ({} patches total):'.format(total),
            '',
        ]
        for area in areas_sorted:
            patch_count = area_counts[area]
            ret.append('- {}: {}'.format(area, patch_count))
        ret.append('')

        return ret

    def _individual_changes(self, analysis):
        area_logs = {}
        for area, patches in analysis.upstream_area_patches.items():
            area_logs[area] = self._upstream_area_message(area, patches)

        return (
            ['Individual Changes',
             '==================',
             ''] +
            self._areas_summary(analysis) +
            [area_logs[area] for area in sorted(area_logs)])

    def _osf_outstanding(self, analysis):
        outstanding = analysis.osf_outstanding_patches
        likely_merged = analysis.osf_merged_patches
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
            return ret

        addl('Likely merged OSF patches:', True)
        addl('IMPORTANT: You probably need to revert these and re-run!', True)
        addl('           Make sure to check the above as well; these are',
             True)
        addl("           guesses that aren't always right.", True)
        addl('', True)
        for sl, commits in likely_merged.items():
            addl('- "{}", likely merged as one of:'.format(sl), True)
            for c in commits:
                addl('\t- {} {}'.format(commit_shortsha(c),
                                        commit_shortlog(c)),
                     True)
            addl('', True)

        return ret


class ZephyrTextFormatter(ZephyrTextFormatMixin, ZephyrOutputFormatter):
    '''Plain text, for mergeup commit messages.

    This includes a summary of OSF outstanding patches, and may
    print warnings if there are likely reverted OSF commits'''

    @classmethod
    def names(cls):
        return ['txt', 'text/plain']

    def get_output(self, analysis):
        return self.do_get_output(analysis, include_osf_outstanding=True)


class ZephyrMarkdownFormatter(ZephyrTextFormatMixin, ZephyrOutputFormatter):
    '''Markdown, for blog posts.

    This doesn't include a summary of outstanding OSF commits.'''

    @classmethod
    def names(cls):
        return ['md', 'text/markdown']

    def get_output(self, analysis):
        return self.do_get_output(analysis, include_osf_outstanding=False)

    def upstream_commit_line(self, commit):
        '''Get a line about the given upstream commit.'''
        full_oid = str(commit.oid)
        link = ('https://github.com/zephyrproject-rtos/zephyr/commit/' +
                full_oid)
        return '- [{}]({}) {}'.format(commit_shortsha(commit),
                                      link,
                                      commit_shortlog(commit))


def dump_unknown_commit_help(unknown_commits):
    print("Error: can't build mergeup log message.",
          'The following commits have unknown areas:',
          file=sys.stderr)
    print(file=sys.stderr)
    for c in unknown_commits:
        print('- {} {}'.format(commit_shortsha(c), commit_shortlog(c)),
              file=sys.stderr)
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


def main(args):
    repo_path = args.repo
    if repo_path is None:
        repo_path = os.getcwd()

    analyzer = ZephyrRepoAnalyzer(repo_path, args.osf_ref, args.upstream_ref,
                                  args.sha_to_area)
    try:
        analysis = analyzer.analyze()
    except UnknownCommitsError as e:
        dump_unknown_commit_help(e.args)
        sys.exit(1)

    try:
        formatter_cls = ZephyrOutputFormatter.get_by_name(args.format)
    except ValueError as e:
        # TODO add some logic to print the choices
        print('Error:', '\n'.join(e.args), file=sys.stderr)
        sys.exit(1)

    formatter = formatter_cls()
    output = formatter.get_output(analysis)
    print(output)


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
        ('Drivers', 'sensors/lsm5dsl: Fix SPI API usage'),
        ('Drivers',
         "hid: core: truncated wLength if it doesn't match report descriptor "
         "size"),
        ('Drivers',
         'uart_pipe: re-work the RX function to match the API '
         'and work with USB.'),
        ('Drivers', 'netusb: rndis: Add more debugs'),
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
        ('Scripts', 'kconfiglib: Update to 2259d353426f1'),
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
                        help='''OSF ref (commit-ish) to analyze upstream
                        differences with. Default is osf-dev/master.''')
    parser.add_argument('--upstream-ref', default='upstream/master',
                        help='''Upstream ref (commit-ish) whose differences
                        with osf-ref to analyze. Default is
                        upstream/master.''')
    parser.add_argument('-A', '--set-area', default=[], action='append',
                        help='''Format is sha:Area; associates an area with
                        a commit SHA. Use --areas to print all areas.''')
    parser.add_argument('-f', '--format', default='md',
                        help='''Output format, default is md
                        (text/markdown).''')
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
