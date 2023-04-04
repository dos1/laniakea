# -*- coding: utf-8 -*-
#
# Copyright (C) 2018-2022 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: LGPL-3.0+

import sys
from argparse import ArgumentParser

from .spearsengine import SpearsEngine

__mainfile = None


def check_print_version(options):
    if options.show_version:
        from laniakea import __version__

        print(__version__)
        sys.exit(0)


def check_verbose(options):
    if options.verbose:
        from laniakea.logging import set_verbose

        set_verbose(True)


def command_update(options):
    '''Update Britney and its configuration'''

    engine = SpearsEngine()

    ret = engine.update_config(options.update_britney)
    if not ret:
        sys.exit(2)


def command_migrate(options):
    '''Run a Britney migration'''

    engine = SpearsEngine()

    ret = engine.run_migration(options.repo_name, options.suite1, options.suite2)
    if not ret:
        sys.exit(2)


def create_parser():
    '''Create Spears CLI argument parser'''

    parser = ArgumentParser(description='Migrate packages between suites')
    subparsers = parser.add_subparsers(dest='sp_name', title='subcommands')

    # generic arguments
    parser.add_argument('--verbose', action='store_true', dest='verbose', help='Enable debug messages.')
    parser.add_argument(
        '--version', action='store_true', dest='show_version', help='Display the version of Laniakea itself.'
    )

    sp = subparsers.add_parser('update', help='Update the copy of Britney and its configuration.')
    sp.add_argument(
        '--update-britney', action='store_true', dest='update_britney', help='Fetch new Britney code form Git.'
    )
    sp.set_defaults(func=command_update)

    sp = subparsers.add_parser(
        'migrate', help='Run migration. If suites are omitted, migration is run for all targets.'
    )
    sp.add_argument('--repo', dest='repo_name', help='Act only on the repository with this name.')
    sp.add_argument('suite1', type=str, help='The source suite.', nargs='?')
    sp.add_argument('suite2', type=str, help='The target suite.', nargs='?')
    sp.set_defaults(func=command_migrate)

    return parser


def run(mainfile, args):
    from laniakea.utils.misc import set_process_title, ensure_laniakea_master_user

    set_process_title('laniakea-spears')
    global __mainfile
    __mainfile = mainfile

    if len(args) == 0:
        print('Need a subcommand to proceed!')
        sys.exit(1)

    parser = create_parser()

    args = parser.parse_args(args)
    check_print_version(args)
    check_verbose(args)

    ensure_laniakea_master_user(warn_only=True)
    args.func(args)
