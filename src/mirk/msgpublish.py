# -*- coding: utf-8 -*-
#
# Copyright (C) 2018-2022 Matthias Klumpp <matthias@tenstral.net>
#
# SPDX-License-Identifier: LGPL-3.0+

import os
import json
import logging as log
from typing import Any
from fnmatch import fnmatch

import zmq
import zmq.asyncio

import laniakea.typing as T
from laniakea.msgstream import (
    verify_event_message,
    create_event_listen_socket,
    event_message_is_valid_and_signed,
)

from .config import MirkConfig
from .messages import (
    message_templates,
    render_template_colors,
    message_prestyle_event_data,
)
from .matrix_client import MirkMatrixClient

# These messages are disabled for now, because there are many of them and they generate a lot of noise.
# TODO: Make displaying these a configurable setting.
EVENT_TAG_BLACKLIST = {'_lk.debcheck.issue-resolved', '_lk.debcheck.issue-found'}


class RoomSettings:
    filter_rules: list[dict[str, Any]] = []


def filter_entry_matches(fentry, data):
    if type(fentry) is list:
        for fe in fentry:
            if fnmatch(data, fe):
                return True
        return False
    else:
        return fnmatch(data, fentry)


def filter_rules_match_event(rules, event):
    '''
    Check if our filter rules :rules match the data present
    in :event
    '''

    # create a flatter data structure for easy matching
    flat_data = event['data']
    flat_data['tag'] = event['tag']

    # we have filter rules
    rule_matched = False
    for rule in rules:
        match_okay = False
        for key, filter_value in rule.items():
            event_value = flat_data.get(key)
            if not event_value:
                continue  # we can ignore this rule here
            if type(event_value) is str:
                match_okay = filter_entry_matches(filter_value, event_value)
                if not match_okay:
                    break
            elif type(event_value) is list:
                for evs in event_value:
                    if not type(evs) is str:
                        continue
                    match_okay = filter_entry_matches(filter_value, evs)
                    if not match_okay:
                        break
                if not match_okay:
                    break
        if match_okay:
            rule_matched = True
            break

    return rule_matched


class MatrixPublisher:
    '''
    Publish messages from the Laniakea Message Stream in Matrix rooms.
    '''

    def __init__(self):
        from glob import glob

        import systemd.daemon

        from laniakea.msgstream import keyfile_read_verify_key
        from laniakea.localconfig import LocalConfig

        self._zctx = zmq.asyncio.Context()
        self._lhsub_socket = create_event_listen_socket(self._zctx)
        self._mconf = MirkConfig()
        self._mconf.load()

        # Read all the keys that we trust, to verify messages
        # TODO: Implement auto-reloading of valid keys list if directory changes
        self._trusted_keys = {}
        for keyfname in glob(os.path.join(LocalConfig().trusted_curve_keys_dir, '*')):
            signer_id, verify_key = keyfile_read_verify_key(keyfname)
            if signer_id and verify_key:
                self._trusted_keys[signer_id] = verify_key

        self._mclient = MirkMatrixClient(self._mconf)

        # we're ready now
        systemd.daemon.notify('READY=1')

    def _tag_data_to_html_message(self, tag, event):
        '''Convert the JSON message into a nice HTML string for display.'''

        sdata = event.copy()
        sdata['url_webswview'] = self._mconf.webswview_url
        sdata['url_webview'] = self._mconf.webview_url

        sdata = message_prestyle_event_data(sdata)

        text = ''
        templ = message_templates.get(tag)
        if templ:
            try:
                if callable(templ):
                    text = templ(tag, sdata)
                else:
                    text = render_template_colors(templ).format(**sdata)
            except Exception as e:
                text = (
                    '[<font color="#ed1515">FORMATTING_FAILED</font>] '
                    + str(e)
                    + ' :: tag: '
                    + tag
                    + ' data: <code>'
                    + str(sdata)
                    + '</code>'
                )
        else:
            text = 'Received event type <code>{}</code> with data <code>{}</code>'.format(tag, str(event))

        return text

    async def _on_event_received(self, event):
        tag = event['tag']
        data = event['data']

        # don't react to blacklisted tags
        if tag in EVENT_TAG_BLACKLIST:
            return

        signatures = event.get('signatures')
        signature_trusted = False
        for signer in signatures.keys():
            key = self._trusted_keys.get(signer)
            if not key:
                continue
            try:
                verify_event_message(signer, event, key, assume_valid=True)
            except Exception as e:
                log.info('Invalid signature on event ({}): {}'.format(str(e), str(event)))
                break

            # if we are here, we verified a signature without issues, which means
            # the message is legit and we can sign it ourselves and publish it
            signature_trusted = True
            break

        if signature_trusted:
            text = self._tag_data_to_html_message(tag, data)
        else:
            if self._mconf.allow_unsigned:
                text = self._tag_data_to_html_message(tag, data)
                text = '[<font color="#ed1515">VERIFY_FAILED</font>] ' + text
            else:
                log.info('Unable to verify signature on event: {}'.format(str(event)))
                return

        await self._rooms_publish_text(event, text)

    async def _rooms_publish_text(self, event: dict[str, T.Any], text: str):
        """Publish raw message text in all rooms."""

        if not text:
            return

        for room_id, settings in self._rooms.items():
            filter_rules = settings.filter_rules
            if not filter_rules:
                # no filter rules means we emit everything
                await self._mclient.send_simple_html(room_id, self._mconf.message_prefix + text)
                continue

            # check if we are allowed to send this message to the particular room,
            # and then send it
            if filter_rules_match_event(filter_rules, event):
                await self._mclient.send_simple_html(room_id, self._mconf.message_prefix + text)

    def stop(self):
        self._running = False
        self._mclient.stop()

    async def run(self):
        '''Run Matrix Bot operations, forever.'''

        # log into matrix
        await self._mclient.login()

        # prepare room settings
        self._rooms = {}
        for room_id, rsdata in self._mconf.rooms.items():
            settings = RoomSettings()
            settings.filter_rules = rsdata.get('Filter', [])
            self._rooms[room_id] = settings

        log.info('Ready to publish information')
        self._running = True

        while self._running:
            mparts = await self._lhsub_socket.recv_multipart()
            if len(mparts) != 2:
                log.info('Received message with odd length: %s', len(mparts))
            msg_b = mparts[1]
            msg_s = str(msg_b, 'utf-8', 'replace')

            try:
                event = json.loads(msg_s)
            except json.JSONDecodeError as e:
                # we ignore invalid requests
                log.info('Received invalid JSON message: %s (%s)', msg_s if len(msg_s) > 1 else msg_b, str(e))
                continue

            # check if the message is actually valid and can be processed
            if not event_message_is_valid_and_signed(event):
                # we currently just silently ignore invalid submissions, no need to spam
                # the logs in case some bad actor flood server with spam
                log.debug('Invalid message ignored.')
                continue

            await self._on_event_received(event)
