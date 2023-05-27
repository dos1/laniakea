# -*- coding: utf-8 -*-
#
# Copyright (C) 2016-2022 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: LGPL-3.0+

import os
import subprocess
from datetime import datetime

import yaml

import laniakea.typing as T
from laniakea.db import PackageType, PackageIssue, DebcheckIssue, PackageConflict
from laniakea.utils import process_file_lock
from laniakea.logging import log
from laniakea.reporeader import RepositoryReader
from laniakea.localconfig import LocalConfig


class DoseDebcheck:
    """
    Analyze the archive's dependency chain.
    """

    def __init__(self, session, repo):
        lconf = LocalConfig()
        self._repo = repo
        self._repo_reader = RepositoryReader(os.path.join(lconf.archive_root_dir, repo.name), repo.name, entity=repo)
        self._repo_reader.set_trusted(True)
        self._session = session

    def _execute_dose(self, dose_exe, args, files: list[str] = None):
        if not files:
            files = []

        yaml_data = ''
        cmd = [dose_exe]
        cmd.extend(args)
        cmd.extend(files)

        pipe = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        while True:
            line = pipe.stdout.readline()
            if line == '' and pipe.poll() is not None:
                break
            if line:
                yaml_data += line

        if not yaml_data.startswith('output-version'):
            # the output is weird, assume an error
            return False, yaml_data + '\n' + pipe.stderr.read()

        return True, yaml_data

    def _get_full_index_info(self, suite, arch, sources=False):
        '''
        Get a list of index files for the specific suite and architecture,
        for all components, as well as all the suites it depends on.

        The actual indices belonging to the suite are added as "foreground" (fg), the
        ones of the dependencies are added as "background" (bg).
        '''

        res = {'fg': [], 'bg': []}
        bin_arch = suite.primary_architecture

        for component in suite.components:
            if sources:
                fname = self._repo_reader.index_file(suite, os.path.join(component.name, 'source', 'Sources.xz'))
                if fname:
                    res['fg'].append(fname)

                fname = self._repo_reader.index_file(
                    suite, os.path.join(component.name, 'binary-{}'.format(arch.name), 'Packages.xz')
                )
                if fname:
                    res['bg'].append(fname)
            else:
                fname = self._repo_reader.index_file(
                    suite, os.path.join(component.name, 'binary-{}'.format(arch.name), 'Packages.xz')
                )
                if fname:
                    res['fg'].append(fname)

            if arch.name == 'all':
                fname = self._repo_reader.index_file(
                    suite, os.path.join(component.name, 'binary-{}'.format(bin_arch.name), 'Packages.xz')
                )
                if fname:
                    res['bg'].append(fname)
            else:
                fname = self._repo_reader.index_file(suite, os.path.join(component.name, 'binary-all', 'Packages.xz'))
                if fname:
                    res['bg'].append(fname)

        # add base suite packages to the background
        for parent in suite.parents:
            parent_indices = self._get_full_index_info(parent, arch, False)
            res['bg'].extend(parent_indices['bg'])
            res['bg'].extend(parent_indices['fg'])

        return res

    def _generate_build_depcheck_yaml(self, suite):
        '''
        Get Dose YAML data for build dependency issues in the selected suite.
        '''

        arch_issue_map = {}
        for arch in suite.architectures:
            if arch.name == 'all':
                # we ignore arch:all for build dependency checks, as "all"-only packages will show up for other
                # architectures as well, adn we can not build a package on a purely arch:all installation anyway.
                continue

            # fetch source-package-centric index list
            indices = self._get_full_index_info(suite, arch, True)
            if not indices['fg']:
                raise Exception(
                    'Unable to get any indices for {}/{} to check for dependency issues.'.format(suite.name, arch.name)
                )

            dose_args = [
                '--quiet',
                '--latest=1',
                '-e',
                '-f',
                '--summary',
                '--deb-emulate-sbuild',
                '--deb-native-arch={}'.format(arch.name),
            ]

            # run builddepcheck
            success, data = self._execute_dose('dose-builddebcheck', dose_args, indices['bg'] + indices['fg'])
            if not success:
                raise Exception('Unable to run Dose (builddebcheck) for {}/{}: {}'.format(suite.name, arch.name, data))
            arch_issue_map[arch.name] = data

        return arch_issue_map

    def _generate_depcheck_yaml(self, suite):
        '''
        Get Dose YAML data for build installability issues in the selected suite.
        '''

        arch_issue_map = {}
        for arch in suite.architectures:
            # fetch binary-package index list
            indices = self._get_full_index_info(suite, arch, False)
            if not indices['fg']:
                raise Exception(
                    'Unable to get any indices for {}/{} to check for dependency issues.'.format(suite.name, arch.name)
                )

            dose_args = [
                '--quiet',
                '--latest=1',
                '-e',
                '-f',
                '--summary',
                '--deb-native-arch={}'.format(suite.primary_architecture.name if arch.name == 'all' else arch.name),
            ]

            # run depcheck
            indices_args = []
            for f in indices['bg']:
                indices_args.append('--bg={}'.format(f))
            for f in indices['fg']:
                indices_args.append('--fg={}'.format(f))
            success, data = self._execute_dose('dose-debcheck', dose_args, indices_args)
            if not success:
                log.error(
                    'Dose debcheck command failed: ' + ' '.join(dose_args) + ' ' + ' '.join(indices_args) + '\n' + data
                )
                raise Exception('Unable to run Dose for {}/{}: {}'.format(suite.name, arch.name, data))

            arch_issue_map[arch.name] = data

        return arch_issue_map

    def _dose_yaml_to_issues(self, yaml_data, suite, arch_name, package_type_override=None):
        """Convert a DOSE YAML report into a sequence of DebcheckIssue entities and add them to the database."""

        def set_basic_package_info(v: T.Union[PackageIssue, DebcheckIssue], entry):
            if 'type' in entry and entry['type'] == 'src':
                v.package_type = PackageType.SOURCE
            else:
                v.package_type = PackageType.BINARY

            v.package_name = str(entry['package'])
            v.package_version = str(entry['version'])
            architectures_raw = str(entry['architecture']).split(',')
            if v.package_type == PackageType.BINARY:
                v.architectures = architectures_raw
                try:
                    v.architectures.remove('any')
                except ValueError:
                    pass
                if arch_name not in v.architectures:
                    v.architectures.append(arch_name)
            else:
                # for source packages we only register the selected architecture, and 'all'
                if 'all' in architectures_raw:
                    v.architectures = [arch_name, 'all']
                else:
                    v.architectures = [arch_name]

        new_issues = []
        all_issues = []
        yroot = yaml.safe_load(yaml_data)
        report = yroot['report']
        arch_is_all = arch_name == 'all'

        # if the report is empty, we have no issues to generate and can quit
        if not report:
            return new_issues, all_issues

        for entry in report:
            if not arch_is_all:
                # we ignore entries from "all" unless we are explicitly reading information
                # for that fake architecture.
                if entry['architecture'] == 'all':
                    continue

            issue = DebcheckIssue()
            issue.time = datetime.utcnow()
            missing = []
            conflicts = []
            set_basic_package_info(issue, entry)
            if package_type_override:
                issue.package_type = package_type_override
            issue_uuid = DebcheckIssue.generate_uuid(issue, self._repo, suite)

            is_new = False
            existing_issue = self._session.query(DebcheckIssue).filter(DebcheckIssue.uuid == issue_uuid).one_or_none()
            if existing_issue:
                # update the existing issue
                issue = existing_issue
                set_basic_package_info(issue, entry)
                if package_type_override:
                    issue.package_type = package_type_override
            else:
                # add the new issue
                is_new = True
                issue.uuid = issue_uuid
                issue.repo = self._repo
                issue.suite = suite
                self._session.add(issue)

            reasons = entry['reasons']
            for reason in reasons:
                if 'missing' in reason:
                    # we have a missing package issue
                    ymissing = reason['missing']['pkg']
                    pkgissue = PackageIssue()
                    set_basic_package_info(pkgissue, ymissing)
                    pkgissue.unsat_dependency = ymissing['unsat-dependency']

                    missing.append(pkgissue)
                elif 'conflict' in reason:
                    # we have a conflict in the dependency chain
                    yconflict = reason['conflict']
                    conflict = PackageConflict()
                    conflict.pkg1 = PackageIssue()
                    conflict.pkg2 = PackageIssue()
                    conflict.depchain1 = []
                    conflict.depchain2 = []

                    set_basic_package_info(conflict.pkg1, yconflict['pkg1'])
                    if 'unsat-conflict' in yconflict['pkg1']:
                        conflict.pkg1.unsat_conflict = yconflict['pkg1']['unsat-conflict']

                    set_basic_package_info(conflict.pkg2, yconflict['pkg2'])
                    if 'unsat-conflict' in yconflict['pkg2']:
                        conflict.pkg2.unsat_conflict = yconflict['pkg2']['unsat-conflict']

                    # parse the depchain
                    if 'depchain1' in yconflict:
                        for ypkg in yconflict['depchain1'][0]['depchain']:
                            pkgissue = PackageIssue()
                            set_basic_package_info(pkgissue, ypkg)
                            pkgissue.depends = ypkg.get('depends')
                            conflict.depchain1.append(pkgissue)

                    if 'depchain2' in yconflict:
                        for ypkg in yconflict['depchain2'][0]['depchain']:
                            pkgissue = PackageIssue()
                            set_basic_package_info(pkgissue, ypkg)
                            pkgissue.depends = ypkg.get('depends')
                            conflict.depchain2.append(pkgissue)

                    conflicts.append(conflict)
                else:
                    raise Exception('Found unknown dependency issue: ' + str(reason))

                issue.missing = missing
                issue.conflicts = conflicts

            all_issues.append(issue)
            if is_new:
                new_issues.append(issue)

        return new_issues, all_issues

    def fetch_build_depcheck_issues(self, suite) -> tuple[list[DebcheckIssue], list[DebcheckIssue]]:
        '''Get a list of build-dependency issues affecting the suite'''

        with process_file_lock('publish_{}-{}'.format(self._repo.name, suite.name), wait=True):
            issues_yaml = self._generate_build_depcheck_yaml(suite)

        new_issues = []
        all_issues = []
        for arch_name, yaml_data in issues_yaml.items():
            new_i, all_i = self._dose_yaml_to_issues(yaml_data, suite, arch_name, PackageType.SOURCE)
            new_issues.extend(new_i)
            all_issues.extend(all_i)

        return new_issues, all_issues

    def fetch_depcheck_issues(self, suite) -> tuple[list[DebcheckIssue], list[DebcheckIssue]]:
        '''Get a list of dependency issues affecting the suite'''

        with process_file_lock('publish_{}-{}'.format(self._repo.name, suite.name), wait=True):
            issues_yaml = self._generate_depcheck_yaml(suite)

        new_issues = []
        all_issues = []
        for arch_name, yaml_data in issues_yaml.items():
            new_i, all_i = self._dose_yaml_to_issues(yaml_data, suite, arch_name, PackageType.BINARY)
            new_issues.extend(new_i)
            all_issues.extend(all_i)

        return new_issues, all_issues
