# -*- coding: utf-8 -*-
#
# Copyright (C) 2016-2022 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: LGPL-3.0+

import enum
from uuid import uuid4
from datetime import datetime

from sqlalchemy import (
    Enum,
    Text,
    Column,
    String,
    Boolean,
    Integer,
    DateTime,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import backref, relationship
from sqlalchemy.dialects.postgresql import ARRAY

from .base import UUID, Base, DebVersion
from .archive import ArchiveRepository


class SynchrotronSource(Base):
    '''
    Definition of a foreign suite to sync packages from.
    '''

    __tablename__ = 'synchrotron_sources'
    __table_args__ = (UniqueConstraint('os_name', 'suite_name', name='_os_suite_uc'),)

    id = Column(Integer, primary_key=True)

    os_name = Column(Text(), nullable=False)  # Name of the source OS (usually "Debian")
    suite_name = Column(String(256), nullable=False)
    architectures = Column(ARRAY(String(64)))
    components = Column(ARRAY(String(128)))
    repo_url = Column(Text(), nullable=False)


class SynchrotronConfig(Base):
    '''
    Configuration for automatic synchrotron tasks.
    '''

    __tablename__ = 'synchrotron_config'
    __table_args__ = (UniqueConstraint('repo_id', 'source_id', 'destination_suite_id', name='_repo_source_target_uc'),)

    id = Column(Integer, primary_key=True)

    repo_id = Column(Integer, ForeignKey('archive_repositories.id'), nullable=False)
    repo: ArchiveRepository = relationship('ArchiveRepository')

    source_id = Column(Integer, ForeignKey('synchrotron_sources.id'), nullable=False)
    source = relationship('SynchrotronSource')

    destination_suite_id = Column(Integer, ForeignKey('archive_suites.id'), nullable=False)
    destination_suite = relationship('ArchiveSuite', backref=backref('synchrotron_configs', cascade='all, delete'))

    sync_enabled = Column(Boolean(), default=True)  # true if syncs should happen
    sync_auto_enabled = Column(Boolean(), default=False)  # true if syncs should happen automatically
    sync_binaries = Column(Boolean(), default=False)  # true if we should also sync binary packages
    auto_cruft_remove = Column(Boolean(), default=True)  # true if we should automatically try to remove cruft in target


class SyncBlacklistEntry(Base):
    '''
    Synchrotron blacklist
    '''

    __tablename__ = 'synchrotron_blacklist'

    uuid = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    config_id = Column(Integer, ForeignKey('synchrotron_config.id'))
    config = relationship('SynchrotronConfig', cascade='all, delete')

    pkgname = Column(String(256))  # Name of the blacklisted package
    time_created = Column(DateTime(), default=datetime.utcnow)  # Time when the package was blacklisted
    reason = Column(Text())  # Reason why the package is blacklisted

    user = Column(String(256))  # Person who marked this to be ignored


class SynchrotronIssueKind(enum.IntEnum):
    '''
    Kind of a Synchrotron issue.
    '''

    UNKNOWN = 0
    NONE = 1
    MERGE_REQUIRED = 2
    MAYBE_CRUFT = 3
    SYNC_FAILED = 4
    REMOVAL_FAILED = 5

    def to_string(self):
        if self.value == self.NONE:
            return 'none'
        if self.value == self.MERGE_REQUIRED:
            return 'merge-required'
        if self.value == self.MAYBE_CRUFT:
            return 'maybe-cruft'
        if self.value == self.SYNC_FAILED:
            return 'sync-failed'
        if self.value == self.REMOVAL_FAILED:
            return 'removal-failed'
        return 'SynchrotronIssueKind.' + str(self.name)

    def __str__(self):
        return self.to_string()


class SynchrotronIssue(Base):
    '''
    Hints about why packages are not synchronized with a source distribution/suite.
    '''

    __tablename__ = 'synchrotron_issues'

    uuid = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    config_id = Column(Integer, ForeignKey('synchrotron_config.id'), nullable=False)
    config = relationship('SynchrotronConfig', backref=backref('issues', cascade='all, delete'))

    time_created = Column(DateTime(), default=datetime.utcnow)  # Time when this excuse was created

    kind = Column(Enum(SynchrotronIssueKind))  # Kind of this issue, and usually also the reason for its existence.

    package_name = Column(String(200))  # Name of the source package that is to be synchronized

    source_suite = Column(String(200))  # Source suite of this package, usually the one in Debian
    target_suite = Column(String(200))  # Target suite of this package, from the target distribution

    source_version = Column(DebVersion())  # package version to be synced
    target_version = Column(DebVersion())  # version of the package in the target suite and repo, to be overriden

    details = Column(Text())  # additional information text about the issue (usually a log excerpt)
