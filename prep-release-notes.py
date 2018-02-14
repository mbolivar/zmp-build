#!/usr/bin/env python3

import collections
from datetime import datetime, timezone, timedelta
from io import StringIO
from itertools import dropwhile, takewhile
import os
from os.path import abspath, join
import textwrap

import click
import pygit2

DEFAULT_INDENT = 12
MERGEUP_SAUCE_TAGS = ['LTD mergeup', 'OSF mergeup']
HIGHLIGHTS = ['''highlights
----------''',
              '''highlights
==========''']
UPSTREAM_CHANGES = ['''upstream changes
----------------''',
                    '''upstream changes
================''']


def is_mergeup_commit(commit):
    short = commit.message.splitlines()[0]
    return any(tag in short for tag in MERGEUP_SAUCE_TAGS)


def mergeup_commits(repository_path, baseline_commit):
    repository = pygit2.init_repository(repository_path)
    sort = pygit2.GIT_SORT_TIME | pygit2.GIT_SORT_TOPOLOGICAL
    walker = repository.walk(repository.head.target, sort)
    walker.hide(baseline_commit)
    return [c for c in walker if is_mergeup_commit(c)]


def mergeup_highlights(commit):
    def is_not_highlights(paragraph):
        return paragraph.lower() not in HIGHLIGHTS

    def is_not_upstream_changes(paragraph):
        return paragraph.lower() not in UPSTREAM_CHANGES

    paragraphs = commit.message.split('\n\n')
    start = dropwhile(is_not_highlights, paragraphs)
    hls = [hl for hl in takewhile(is_not_upstream_changes, start)]
    return hls[1:]  # skip HIGHLIGHTS itself


def commit_date(commit):
    author_timestamp = float(commit.author.time)
    author_time_offset = commit.author.offset
    author_tz = timezone(timedelta(minutes=author_time_offset))
    return datetime.fromtimestamp(author_timestamp, author_tz)


def repo_mergeup_highlights(repo, baseline, yaml_indent):
    out = StringIO()

    mergeups = mergeup_commits(repo, baseline)
    highlights = collections.OrderedDict((m, mergeup_highlights(m))
                                         for m in mergeups)
    missing = []
    # The indentation level is to the '-' before 'highlights':
    #
    # - heading: TODO
    #   summary: |
    #     text_wrapped_by_this_wrapper
    #
    # Thus the highlights text is wrapped at indent + 4.
    base_indent = ' ' * yaml_indent
    content_indent = ' ' * (yaml_indent + 4)
    wrapper = textwrap.TextWrapper(initial_indent=content_indent,
                                   subsequent_indent=content_indent)

    for m, hls in highlights.items():
        if not hls:
            missing.append(m)
            continue
        print('# From mergeup {} on {}:'.format(str(m.id)[:7], commit_date(m)),
              file=out)
        for hl in hls:
            print('{}- heading: TODO'.format(base_indent), file=out)
            print('{}  summary: |'.format(base_indent), file=out)
            print(wrapper.fill(hl), file=out)
            print(file=out)
    if missing:
        print(file=out)
        print('WARNING: the following mergeup commit(s) had no highlights.',
              file=out)
        for m in missing:
            print('ID: {}'.format(m.id), file=out)
            print('Date: {}'.format(commit_date(m)), file=out)
            print('Message:', file=out)
            print(textwrap.indent(m.message, '\t'), file=out)

    return out.getvalue()


@click.command()
@click.option('-z', '--zephyr-baseline', required=True,
              help='Zephyr baseline commit')
@click.option('-m', '--mcuboot-baseline', required=True,
              help='MCUboot baseline commit')
@click.option('--zmp', help='ZMP installation directory, default is cwd')
@click.option('--yaml-indent', type=int, default=DEFAULT_INDENT,
              help='YAML indentation')
def main(zephyr_baseline, mcuboot_baseline, zmp, yaml_indent):
    if zmp is None:
        zmp = abspath(os.getcwd())
    zmp = abspath(zmp)
    zephyr = join(zmp, 'zephyr')
    mcuboot = join(zmp, 'mcuboot')

    zephyr_highlights = repo_mergeup_highlights(zephyr, zephyr_baseline,
                                                yaml_indent)
    mcuboot_highlights = repo_mergeup_highlights(mcuboot, mcuboot_baseline,
                                                 yaml_indent)

    print('#', '=' * 70)
    print('# Zephyr highlights:')
    print(zephyr_highlights)

    print('#', '=' * 70)
    print('# MCUboot highlights:')
    print(mcuboot_highlights)

    print('#', '=' * 70)
    print('# WARNING: no release notes for sample applications above')


if __name__ == '__main__':
    main()
