# -*- coding: utf-8 -*-
#
# Copyright (C) 2020-2022 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: LGPL-3.0+

import os
import shutil
import typing as T
import hashlib
import subprocess

from apt_pkg import Hashes
from debian.deb822 import Sources, Packages

from laniakea import LocalConfig
from laniakea.db import (
    DebType,
    NewPolicy,
    ArchiveFile,
    PackageInfo,
    BinaryPackage,
    SourcePackage,
    ArchiveUploader,
    PackageOverride,
    ArchiveComponent,
    ArchiveRepository,
    ArchiveArchitecture,
    ArchiveQueueNewEntry,
    ArchiveVersionMemory,
    ArchiveRepoSuiteSettings,
)
from laniakea.logging import log
from laniakea.archive.utils import (
    UploadException,
    split_epoch,
    pool_dir_from_name,
    check_overrides_source,
    checksums_list_to_file,
    parse_package_list_str,
    register_package_overrides,
)
from laniakea.archive.changes import InvalidChangesException, parse_changes


class ArchiveImportError(Exception):
    """Import of a package into the archive failed."""


def pop_split(d, key, s):
    """Pop value from dict :d with key :key and split with :s"""
    value = d.pop(key, None)
    if not value:
        return []
    return value.split(s)


def package_mark_published(session, rss: ArchiveRepoSuiteSettings, pkgname: str, version: str):
    """
    Mark package as published. Currently, this only updates the version memory.

    :param session: SQLAlchemy session
    :param rss: RepoSuite settings for this package
    :param pkgname: Package name
    :param version: Package version
    """
    vmem = (
        session.query(ArchiveVersionMemory)
        .filter(ArchiveVersionMemory.repo_id == rss.repo_id, ArchiveVersionMemory.pkgname == pkgname)
        .one_or_none()
    )

    if vmem:
        vmem.highest_version = version
    else:
        vmem = ArchiveVersionMemory()
        vmem.repo = rss.repo
        vmem.pkgname = pkgname
        vmem.highest_version = version
        session.add(vmem)


class PackageImporter:
    """
    Imports packages into the archive directly,
    without performing any policy/permission checks.
    """

    def __init__(self, session, repo_suite_settings: ArchiveRepoSuiteSettings):
        self._session = session
        self._rss = repo_suite_settings

        self._repo_root = self._rss.repo.get_root_dir()
        self._repo_newqueue_root = self._rss.repo.get_new_queue_dir()
        os.makedirs(self._repo_newqueue_root, exist_ok=True)

        self.keep_source_packages = False

    def _copy_or_move(self, src, dst, *, override: bool = False):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if override:
            if os.path.isfile(dst):
                os.unlink(dst)
        shutil.copy(src, dst)
        if not self.keep_source_packages:
            os.unlink(src)

    def _verify_hashes(self, file: ArchiveFile, local_fname: T.Union[os.PathLike, str]):
        """Verifies all known hashes of :file"""
        hashes_checked = 0
        with open(local_fname, 'rb') as f:
            # pylint: disable=not-an-iterable
            for hash in Hashes(f).hashes:  # type: ignore
                if hash.hashtype == 'MD5Sum':
                    hash_okay = file.md5sum == hash.hashvalue
                elif hash.hashtype == 'SHA1':
                    hash_okay = file.sha1sum == hash.hashvalue
                elif hash.hashtype == 'SHA256':
                    hash_okay = file.sha256sum == hash.hashvalue
                elif hash.hashtype == 'SHA512':
                    hash_okay = file.sha512sum == hash.hashvalue
                elif hash.hashtype == 'Checksum-FileSize':
                    hash_okay = file.size == hash.hashvalue
                else:
                    raise ArchiveImportError(
                        'Unknown hash type "{}" - Laniakea likely needs to be adjusted to a new APT version.'.format(
                            hash.hashtype
                        )
                    )
                if not hash_okay:
                    raise ArchiveImportError(
                        '{} checksum validation of "{}" failed (expected {}).'.format(
                            hash.hashtype, file.fname, hash.hashvalue
                        )
                    )
                hashes_checked += 1
        if hashes_checked < 4:
            raise ArchiveImportError('An insufficient amount of hashes was validated for "{}" - this is a bug.')

    def import_source(
        self,
        dsc_fname: T.Union[os.PathLike, str],
        component_name: str = None,
        *,
        new_policy: NewPolicy = NewPolicy.DEFAULT,
    ):
        """Import a source package into the given suite or its NEW queue.

        :param dsc_fname: Path to a source package to import
        :param component_name: Name of the archive component to import into.
        :param skip_new: True if the NEW queue should be skipped and overrides be added automatically.
        """

        log.info('Attempting import of source: {}', dsc_fname)
        dsc_dir = os.path.dirname(dsc_fname)

        p = subprocess.run(
            ['apt-ftparchive', '-q', 'sources', dsc_fname], capture_output=True, check=True, encoding='utf-8'
        )
        src_tf = Sources(p.stdout)

        pkgname = src_tf.pop('Package')
        version = src_tf.pop('Version')

        result = (
            self._session.query(ArchiveVersionMemory.highest_version)
            .filter(
                ArchiveVersionMemory.repo_id == self._rss.repo_id,
                ArchiveVersionMemory.pkgname == pkgname,
                ArchiveVersionMemory.highest_version > version,
            )
            .one_or_none()
        )
        if result:
            raise ArchiveImportError(
                'Unable to import package "{}": '
                'We have already seen higher version "{}" in this repository before.'.format(pkgname, result)
            )

        spkg = SourcePackage(pkgname, version, self._rss.repo)
        spkg.component = self._session.query(ArchiveComponent).filter(ArchiveComponent.name == component_name).one()

        # check if this package is currently in the NEW queue, and if it is just update it
        nq_entry = (
            self._session.query(ArchiveQueueNewEntry)
            .filter(
                ArchiveQueueNewEntry.destination_id == self._rss.suite_id,
                ArchiveQueueNewEntry.package.has(name=spkg.name),
                ArchiveQueueNewEntry.package.has(version=spkg.version),
                ArchiveQueueNewEntry.package.has(repo_id=self._rss.repo_id),
            )
            .one_or_none()
        )
        if nq_entry:
            spkg = nq_entry.package

        spkg.format_version = src_tf.pop('Format')
        spkg.architectures = pop_split(src_tf, 'Architecture', ' ')
        spkg.maintainer = src_tf.pop('Maintainer')
        spkg.uploaders = pop_split(src_tf, 'Uploaders', ', ')

        spkg.build_depends = pop_split(src_tf, 'Build-Depends', ', ')
        spkg.build_depends_indep = pop_split(src_tf, 'Build-Depends-Indep', ', ')
        spkg.build_conflicts = pop_split(src_tf, 'Build-Conflicts', ', ')
        spkg.build_conflicts_indep = pop_split(src_tf, 'Build-Conflicts-Indep', ', ')
        if 'Package-List' in src_tf:
            spkg.expected_binaries = parse_package_list_str(src_tf.pop('Package-List'))
            src_tf.pop('Binary')
        else:
            log.warning(
                'Source package dsc file `{}/{}` had no `Package-List` '
                '- falling back to parsing `Binaries`.'.format(pkgname, version)
            )
            binary_stubs = []
            for b in pop_split(src_tf, 'Binary', ', '):
                pi = PackageInfo()
                pi.name = b
                binary_stubs.append(pi)
            spkg.expected_binaries = binary_stubs

        spkg.directory = pool_dir_from_name(pkgname)
        files = checksums_list_to_file(src_tf.pop('Files'), 'md5')
        files = checksums_list_to_file(src_tf.pop('Checksums-Sha1'), 'sha1', files)
        files = checksums_list_to_file(src_tf.pop('Checksums-Sha256'), 'sha256', files)
        files = checksums_list_to_file(src_tf.pop('Checksums-Sha512', None), 'sha512', files)

        # remove any old file entries, in case we are updating
        # a package that is placed in NEW
        if spkg.files:
            for file in spkg.files:
                fname_full = os.path.join(self._repo_newqueue_root, file.fname)
                if os.path.isfile(fname_full):
                    os.unlink(fname_full)
                self._session.delete(file)
            spkg.files = []
            self._session.flush()

        files_todo = []
        for file in files.values():
            spkg.files.append(file)
            # ensure the files hashes are correct
            self._verify_hashes(file, os.path.join(dsc_dir, file.fname))

            pool_fname = os.path.join(spkg.directory, file.fname)
            file.fname = pool_fname
            file.repo = self._rss.repo
            files_todo.append(file)

        missing_overrides = check_overrides_source(self._session, self._rss, spkg)
        if new_policy == NewPolicy.NEVER_NEW:
            # if we are supposed to skip NEW, we just register the overrides and add the package
            # to its designated suite
            register_package_overrides(self._session, self._rss, missing_overrides)
            spkg.suites.append(self._rss.suite)
            is_new = False
        else:
            if missing_overrides or new_policy == NewPolicy.ALWAYS_NEW:
                # add to NEW queue (update entry or create new one)
                if not nq_entry:
                    nq_entry = ArchiveQueueNewEntry()
                    self._session.add(nq_entry)

                nq_entry.package = spkg
                nq_entry.destination = self._rss.suite
                is_new = True
            else:
                # no missing overrides, the package is good to go
                spkg.suites.append(self._rss.suite)
                is_new = False

        for file in files_todo:
            if is_new:
                # move package to the NEW queue
                pool_fname_full = os.path.join(self._repo_newqueue_root, file.fname)
            else:
                # move package to the archive pool
                pool_fname_full = os.path.join(self._repo_root, file.fname)

            if os.path.exists(pool_fname_full):
                raise ArchiveImportError(
                    'Destination source file `{}` already exists. Can not continue'.format(file.fname)
                )
            self._copy_or_move(os.path.join(dsc_dir, os.path.basename(file.fname)), pool_fname_full, override=is_new)
            self._session.add(file)

        if not is_new and nq_entry:
            # the package is no longer NEW (all overrides are added), but apparently
            # we have a NEW queue entry - get rid of that
            self._session.delete(nq_entry)

        # drop directory key, we don't need it
        src_tf.pop('Directory')

        # store any remaining fields as extra data
        log.debug('Extra data fields for `{}/{}`: {}'.format(pkgname, version, dict(src_tf)))
        spkg.extra_data = dict(src_tf)

        self._session.add(spkg)
        if is_new:
            log.info(
                'Source `{}/{}` for {}/{} added to NEW queue.'.format(
                    spkg.name, spkg.version, self._rss.repo.name, self._rss.suite.name
                )
            )
        else:
            package_mark_published(self._session, self._rss, spkg.name, spkg.version)
            self._rss.changes_pending = True
            log.info(
                'Added source `{}/{}` to {}/{}.'.format(
                    spkg.name, spkg.version, self._rss.repo.name, self._rss.suite.name
                )
            )
            self._session.flush()

    def import_binary(self, deb_fname: T.Union[os.PathLike, str], component_name: str = None):
        """Import a binary package into the given suite or its NEW queue.

        :param deb_fname: Path to a deb/udeb package to import
        :param component_name: Name of the archive component to import into.
        """

        log.info('Attempting import of binary: {}', deb_fname)
        pkg_type = DebType.DEB
        if os.path.splitext(deb_fname)[1] == '.udeb':
            pkg_type = DebType.UDEB

        p = subprocess.run(
            ['apt-ftparchive', '-q', 'packages', deb_fname], capture_output=True, check=True, encoding='utf-8'
        )
        bin_tf = Packages(p.stdout)

        p = subprocess.run(
            ['apt-ftparchive', '-q', 'contents', deb_fname], capture_output=True, check=True, encoding='utf-8'
        )
        filelist_raw = p.stdout.splitlines()

        pkgname = bin_tf.pop('Package')
        version = bin_tf.pop('Version')
        bpkg = BinaryPackage(pkgname, version, self._rss.repo)

        bpkg.architecture = (
            self._session.query(ArchiveArchitecture)
            .filter(ArchiveArchitecture.name == bin_tf.pop('Architecture'))
            .one()
        )
        bpkg.update_uuid()

        bpkg.maintainer = bin_tf.pop('Maintainer')
        bpkg.homepage = bin_tf.pop('Homepage', None)
        bpkg.size_installed = bin_tf.pop('Installed-Size')

        source_info_raw = bin_tf.pop('Source', '')
        if not source_info_raw:
            source_name = pkgname
            source_version = version
        elif '(' in source_info_raw:
            source_name = source_info_raw[0 : source_info_raw.index('(') - 1].strip()
            source_version = source_info_raw[source_info_raw.index('(') + 1 : source_info_raw.index(')')].strip()
        else:
            source_name = source_info_raw
            source_version = version

        # find the source package
        is_new = False
        bpkg.source = (
            self._session.query(SourcePackage)
            .filter(
                SourcePackage.repo_id == self._rss.repo_id,
                SourcePackage.suites.any(id=self._rss.suite_id),
                SourcePackage.name == source_name,
                SourcePackage.version == source_version,
            )
            .one_or_none()
        )
        if not bpkg.source:
            # maybe the package is in NEW?
            nq_entry = (
                self._session.query(ArchiveQueueNewEntry)
                .join(ArchiveQueueNewEntry.package)
                .filter(
                    ArchiveQueueNewEntry.destination_id == self._rss.suite_id,
                    SourcePackage.name == source_name,
                    SourcePackage.version == source_version,
                    SourcePackage.repo_id == self._rss.repo_id,
                )
                .one_or_none()
            )
            if nq_entry:
                bpkg.source = nq_entry.package
            if not bpkg.source:
                raise ArchiveImportError(
                    'Unable to import binary package `{}/{}`: Could not find corresponding source package.'.format(
                        pkgname, version
                    )
                )
            self._session.expunge(bpkg)
            is_new = True

        pool_dir = pool_dir_from_name(bpkg.source.name)
        deb_basename = '{}_{}_{}.{}'.format(
            bpkg.name, split_epoch(bpkg.version)[1], bpkg.architecture.name, str(pkg_type)
        )
        pool_fname = os.path.join(pool_dir, deb_basename)

        af = ArchiveFile(pool_fname, self._rss.repo)
        af.size = bin_tf.pop('Size')
        af.md5sum = bin_tf.pop('MD5sum')
        af.sha1sum = bin_tf.pop('SHA1')
        af.sha256sum = bin_tf.pop('SHA256')
        af.sha512sum = bin_tf.pop('SHA512', None)

        # ensure checksums match
        self._verify_hashes(af, deb_fname)
        if is_new:
            # if this binary belongs to a package in the NEW queue, we don't register it and just move the binary
            # alongside the source package
            pool_fname_full = os.path.join(self._repo_newqueue_root, af.fname)
            self._copy_or_move(deb_fname, pool_fname_full, override=True)

            log.info(
                'Binary `{}/{}` for {}/{} added to NEW queue'.format(
                    bpkg.name, bpkg.version, self._rss.repo.name, self._rss.suite.name
                )
            )
            # nothing left to do, we will not register this package with the database
            return
        else:
            pool_fname_full = os.path.join(self._repo_root, af.fname)

        af.binpkg = bpkg
        bpkg.description = bin_tf.pop('Description')
        bpkg.summary = bpkg.description.split('\n', 1)[0].strip()
        bpkg.description_md5 = hashlib.md5(bpkg.description.encode('utf-8')).hexdigest()

        # we don't need the generated filename value
        bin_tf.pop('Filename')
        # we fetch those from already added overrides
        bin_tf.pop('Priority')
        bin_tf.pop('Section')
        bin_tf.pop('Essential', None)

        # check for override
        override = (
            self._session.query(PackageOverride)
            .filter(PackageOverride.repo_suite_id == self._rss.id, PackageOverride.pkgname == bpkg.name)
            .one_or_none()
        )
        if not override:
            raise ArchiveImportError(
                'Missing override for `{}/{}`: Please process the source package through NEW first before uploading a binary.'.format(
                    pkgname, version
                )
            )
        bpkg.override = override

        # add component
        bpkg.component = self._session.query(ArchiveComponent).filter(ArchiveComponent.name == component_name).one()

        # process contents list
        bpkg.contents = [line.split('\t', 1)[0] for line in filelist_raw]

        bpkg.depends = pop_split(bin_tf, 'Depends', ', ')
        bpkg.pre_depends = pop_split(bin_tf, 'Pre-Depends', ', ')

        bpkg.replaces = pop_split(bin_tf, 'Replaces', ', ')
        bpkg.provides = pop_split(bin_tf, 'Provides', ', ')
        bpkg.recommends = pop_split(bin_tf, 'Recommends', ', ')
        bpkg.suggests = pop_split(bin_tf, 'Suggests', ', ')
        bpkg.enhances = pop_split(bin_tf, 'Enhances', ', ')
        bpkg.conflicts = pop_split(bin_tf, 'Conflicts', ', ')
        bpkg.breaks = pop_split(bin_tf, 'Breaks', ', ')

        bpkg.built_using = pop_split(bin_tf, 'Built-Using', ', ')
        bpkg.multi_arch = bin_tf.pop('Multi-Arch', None)

        # add to target suite
        bpkg.suites.append(self._rss.suite)

        # add (custom) fields that we did no account for
        bpkg.extra_data = dict(bin_tf)

        # copy files and register binary
        if os.path.exists(pool_fname_full):
            raise ArchiveImportError('Destination source file `{}` already exists. Can not continue'.format(af.fname))
        self._copy_or_move(deb_fname, pool_fname_full)

        self._session.add(af)
        self._session.add(bpkg)

        package_mark_published(self._session, self._rss, bpkg.name, bpkg.version)
        self._rss.changes_pending = True
        log.info(
            'Added binary `{}/{}` to {}/{}'.format(bpkg.name, bpkg.version, self._rss.repo.name, self._rss.suite.name)
        )
        self._session.flush()


class UploadHandler:
    """
    Verifies an upload and admits it to the archive if basic checks pass.
    """

    def __init__(self, session, repo: ArchiveRepository):
        self._session = session
        self._repo = repo

        self._lconf = LocalConfig()

        self.keep_source_packages = False

    def process_changes(self, fname: T.Union[os.PathLike, str]) -> T.Tuple[bool, ArchiveUploader, T.Optional[str]]:
        """
        Verify and import an upload by its .changes file.
        The caller should make sure the changes file is located at a safe location.
        :param fname: Path to the .changes file
        :return: A tuple of a boolean indication whether the changes file was processed successfully,
        the archive uploader this upload belongs to, and an optional string explaining the error reason in case of a failure.

        In case of irrecoverable issues (when no uploader can be determined or the signature is missing or invalid)
        an exception is thrown, otherwise a tuple consisting of the status, uploader and error message (if any) is returned.
        """
        from glob import glob

        changes = parse_changes(
            fname,
            keyrings=list(glob(os.path.join(self._lconf.uploaders_keyring_dir, 'pubring.kbx'))),
            require_signature=True,
        )

        uploader: T.Optional[ArchiveUploader] = (
            self._session.query(ArchiveUploader)
            .filter(ArchiveUploader.pgp_fingerprints.any(changes.primary_fingerprint))
            .one_or_none()
        )
        if not uploader:
            raise UploadException(
                'Unable to find registered uploader for fingerprint "{}" for "{}"'.format(
                    changes.primary_fingerprint, os.path.basename(fname)
                )
            )

        if changes.weak_signature:
            return (
                False,
                uploader,
                'The GPG signature on {} is weak, please sign the upload with a stronger key.'.format(
                    os.path.basename(fname)
                ),
            )

        if len(changes.distributions) != 1:
            return (
                False,
                uploader,
                (
                    'Invalid amount of distributions set in this changes file. '
                    'We currently can only handle exactly one target (got {}).'
                ).format(str(changes.distributions)),
            )
        suite_name = changes.distributions[0]

        if changes.sourceful and not uploader.allow_source_uploads:
            return (
                False,
                uploader,
                'This uploader is not permitted to make sourceful uploads.'.format(str(changes.distributions)),
            )

        # fetch the repository-suite config for this package
        rss = (
            self._session.query(ArchiveRepoSuiteSettings)
            .filter(
                ArchiveRepoSuiteSettings.repo.has(id=self._repo.id),
                ArchiveRepoSuiteSettings.suite.has(name=suite_name),
            )
            .one()
        )

        result = (
            self._session.query(ArchiveVersionMemory.highest_version)
            .filter(
                ArchiveVersionMemory.repo_id == rss.repo_id,
                ArchiveVersionMemory.pkgname == changes.source_name,
                ArchiveVersionMemory.highest_version >= changes.changes['Version'],
            )
            .one_or_none()
        )
        if result:
            return (
                False,
                uploader,
                'We have already seen higher or equal version "{}" of source package "{}" in repository "{}" before.'.format(
                    result[0], changes.source_name, self._repo.name
                ),
            )

        # FIXME: We should maybe also preemptively check the binaries and their versions here, rather
        # than possibly uncleanly failing at a later stage.
        # At the moment there is a chance that we partially import the package, if any kind of failure
        # happens at a later stage.

        try:
            files = changes.files
        except InvalidChangesException as e:
            return (
                False,
                uploader,
                'This changes file was invalid: {}.'.format(str(e)),
            )

        if not uploader.allow_binary_uploads:
            for file in files.values():
                if file.fname.endswith(('.deb', '.udeb')):
                    return (
                        False,
                        uploader,
                        'This uploader is not allowed to upload binaries. Please upload a source-only package!.',
                    )

        pi = PackageImporter(self._session, rss)
        pi.keep_source_packages = self.keep_source_packages
        dsc_dir = os.path.dirname(fname)

        # import source package
        for file in files.values():
            if file.fname.endswith('.dsc'):
                if '/' in file.fname:
                    return (
                        False,
                        uploader,
                        'Invalid source package filename: {}'.format(str(file.fname)),
                    )
                try:
                    new_policy = rss.new_policy
                    # uploader policy beats suite policy
                    if uploader.always_review:
                        new_policy = NewPolicy.ALWAYS_NEW
                    pi.import_source(os.path.join(dsc_dir, file.fname), file.component, new_policy=new_policy)
                except Exception as e:
                    return (
                        False,
                        uploader,
                        'Failed to import source package: {}'.format(str(e)),
                    )
                # there should only be one source package per changes file
                break

        # import binary packages
        for file in files.values():
            if file.fname.endswith(('.deb', '.udeb')):
                if '/' in file.fname:
                    return (
                        False,
                        uploader,
                        'Invalid binary package filename: {}'.format(str(file.fname)),
                    )
                try:
                    pi.import_binary(os.path.join(dsc_dir, file.fname), file.component)
                except Exception as e:
                    return (
                        False,
                        uploader,
                        'Failed to import binary package: {}'.format(str(e)),
                    )

        return True, uploader, None
