# Copyright (c) Metaswitch Networks 2015. All rights reserved.
from httplib import HTTPException

import logging
import re
import etcd
from etcd import EtcdConnectionFailed
from socket import timeout as SocketTimeout
import socket
import time

import ijson.backends.yajl2 as ijson

from urllib3 import Timeout
import urllib3
from urllib3.exceptions import ReadTimeoutError
from calico.logutils import logging_exceptions
from calico.datamodel_v1 import READY_KEY

_log = logging.getLogger(__name__)


# Map etcd event actions to the effects we care about.
ACTION_MAPPING = {
    "set": "set",
    "compareAndSwap": "set",
    "create": "set",
    "update": "set",

    "delete": "delete",
    "compareAndDelete": "delete",
    "expire": "delete",
}


class PathDispatcher(object):
    def __init__(self):
        self.handler_root = {}

    def register(self, path, on_set=None, on_del=None):
        _log.info("Registering path %s set=%s del=%s", path, on_set, on_del)
        parts = path.strip("/").split("/")
        node = self.handler_root
        for part in parts:
            m = re.match(r'<(.*)>', part)
            if m:
                capture_name = m.group(1)
                name, node = node.setdefault("capture", (capture_name, {}))
                assert name == capture_name, (
                    "Conflicting capture name %s vs %s" % (name, capture_name)
                )
            else:
                node = node.setdefault(part, {})
        if on_set:
            node["set"] = on_set
        if on_del:
            node["delete"] = on_del

    def handle_event(self, response):
        _log.debug("etcd event %s for key %s", response.action, response.key)
        key_parts = response.key.strip("/").split("/")
        self._handle(key_parts, response, self.handler_root, {})

    def _handle(self, key_parts, response, handler_node, captures):
        while key_parts:
            next_part = key_parts.pop(0)
            if "capture" in handler_node:
                capture_name, handler_node = handler_node["capture"]
                captures[capture_name] = next_part
            elif next_part in handler_node:
                handler_node = handler_node[next_part]
            else:
                _log.debug("No matching sub-handler for %s", response.key)
                return
        # We've reached the end of the key.
        action = ACTION_MAPPING.get(response.action)
        if action in handler_node:
            _log.debug("Found handler for event %s for %s, captures: %s",
                       action, response.key, captures)
            handler_node[action](response, **captures)
        else:
            _log.debug("No handler for event %s on %s. Handler node %s.",
                       action, response.key, handler_node)


class EtcdClientOwner(object):
    """
    Base class for objects that own an etcd Client.  Supports
    reconnecting, optionally copying the cluster ID.
    """

    def __init__(self, etcd_authority):
        super(EtcdClientOwner, self).__init__()
        self.etcd_authority = etcd_authority
        self.client = None
        self.reconnect()

    def reconnect(self, copy_cluster_id=True):
        """
        Reconnects the etcd client.
        """
        if ":" in self.etcd_authority:
            host, port = self.etcd_authority.split(":")
            port = int(port)
        else:
            host = self.etcd_authority
            port = 4001
        if self.client and copy_cluster_id:
            old_cluster_id = self.client.expected_cluster_id
            _log.info("(Re)connecting to etcd. Old etcd cluster ID was %s.",
                      old_cluster_id)
        else:
            _log.info("(Re)connecting to etcd. No previous cluster ID.")
            old_cluster_id = None
        self.client = etcd.Client(
            host=host,
            port=port,
            expected_cluster_id=old_cluster_id
        )


class EtcdWatcher(EtcdClientOwner):
    """
    Helper class for managing an etcd watch session.  Maintains the
    etcd polling index and handles expected exceptions.
    """

    def __init__(self,
                 etcd_authority,
                 key_to_poll):
        super(EtcdWatcher, self).__init__(etcd_authority)
        self.key_to_poll = key_to_poll
        self.next_etcd_index = None

        # Forces a resync after the current poll if set.  Safe to set from
        # another thread.  Automatically reset to False after the resync is
        # triggered.
        self.resync_after_current_poll = False

        # Tells the watcher to stop after this poll.  One-way flag.
        self._stopped = False

        self.dispatcher = PathDispatcher()

    @logging_exceptions(_log)
    def loop(self):
        _log.info("Started %s loop", self)
        while not self._stopped:
            try:
                _log.info("Reconnecting and loading snapshot from etcd...")
                self.reconnect(copy_cluster_id=False)
                self._on_pre_resync()
                try:
                    # Load initial dump from etcd.  First just get all the
                    # endpoints and profiles by id.  The response contains a
                    # generation ID allowing us to then start polling for
                    # updates without missing any.
                    initial_dump = self.load_initial_dump()
                    _log.info("Loaded snapshot from etcd cluster %s, "
                              "processing it...",
                              self.client.expected_cluster_id)
                    self._on_snapshot_loaded(initial_dump)
                    while not self._stopped:
                        # Wait for something to change.
                        response = self.wait_for_etcd_event()
                        if not self._stopped:
                            self.dispatcher.handle_event(response)
                except ResyncRequired:
                    _log.info("Polling aborted, doing resync.")
            except etcd.EtcdException as e:
                # Most likely a timeout or other error in the pre-resync;
                # start over.  These exceptions have good semantic error text
                # so the stack trace would just add log spam.
                _log.error("Unexpected IO or etcd error, triggering "
                           "resync with etcd: %r.", e)
        _log.info("%s.loop() stopped due to self.stop == True", self)

    def register_path(self, *args, **kwargs):
        self.dispatcher.register(*args, **kwargs)

    def wait_for_ready(self, retry_delay):
        _log.info("Waiting for etcd to be ready...")
        ready = False
        while not ready:
            try:
                db_ready = self.client.read(READY_KEY, timeout=10).value
            except etcd.EtcdKeyNotFound:
                _log.warn("Ready flag not present in etcd; felix will pause "
                          "updates until the orchestrator sets the flag.")
                db_ready = "false"
            except etcd.EtcdException as e:
                # Note: we don't log the
                _log.error("Failed to retrieve ready flag from etcd (%r). "
                           "Felix will not receive updates until the "
                           "connection to etcd is restored.", e)
                db_ready = "false"

            if db_ready == "true":
                _log.info("etcd is ready.")
                ready = True
            else:
                _log.info("etcd not ready.  Will retry.")
                time.sleep(retry_delay)
                continue

    def load_initial_dump(self):
        """
        Does a recursive get on the key and returns the result.

        As a side effect, initialises the next_etcd_index field for
        use by wait_for_etcd_event()

        :return: The etcd response object.
        """
        try:
            key = self.client._sanitize_key(self.key_to_poll)
            response = self.client.http.request(
                "GET",
                self.client._base_uri + self.client.key_endpoint + key,
                fields={
                    "recursive": "true"
                },
                redirect=self.client.allow_redirect,
                preload_content=False
            )
            etcd_index = int(response.getheader('x-etcd-index', 1))
            initial_dump = StreamedEtcdResponse(response)
        except (urllib3.exceptions.HTTPError,
                HTTPException,
                socket.error) as e:
            _log.error("Request to etcd failed: %r", e)
            raise etcd.EtcdConnectionFailed(
                "Connection to etcd failed due to %r" % e, cause=e)

        # The etcd_index is the high-water-mark for the snapshot, record that
        # we want to poll starting at the next index.
        self.next_etcd_index = etcd_index + 1
        return initial_dump

    def wait_for_etcd_event(self):
        """
        Polls etcd until something changes.

        Retries on read timeouts and other non-fatal errors.

        :returns: The etcd response object for the change.
        :raises ResyncRequired: If we get out of sync with etcd or hit
            a fatal error.
        """
        assert self.next_etcd_index is not None, \
            "load_initial_dump() should be called first."
        response = None
        while not response:
            if self.resync_after_current_poll:
                _log.debug("Told to resync, aborting poll.")
                self.resync_after_current_poll = False
                raise ResyncRequired()

            try:
                _log.debug("About to wait for etcd update %s",
                           self.next_etcd_index)
                response = self.client.read(self.key_to_poll,
                                            wait=True,
                                            waitIndex=self.next_etcd_index,
                                            recursive=True,
                                            timeout=Timeout(connect=10,
                                                            read=90))
                _log.debug("etcd response: %r", response)
            except EtcdConnectionFailed as e:
                if isinstance(e.cause, (ReadTimeoutError, SocketTimeout)):
                    # This is expected when we're doing a poll and nothing
                    # happened. socket timeout doesn't seem to be caught by
                    # urllib3 1.7.1.  Simply reconnect.
                    _log.debug("Read from etcd timed out (%r), retrying.", e)
                    # Force a reconnect to ensure urllib3 doesn't recycle the
                    # connection.  (We were seeing this with urllib3 1.7.1.)
                    self.reconnect()
                else:
                    # We don't log out the stack trace here because it can
                    # spam the logs heavily if the requests keep failing.
                    # The errors are very descriptive anyway.
                    _log.warning("Connection to etcd failed: %r.", e)
                    # Limit our retry rate in case etcd is down.
                    time.sleep(1)
                    self.reconnect()
            except (etcd.EtcdClusterIdChanged,
                    etcd.EtcdEventIndexCleared) as e:
                _log.warning("Out of sync with etcd (%r).  Reconnecting "
                             "for full sync.", e)
                raise ResyncRequired()
            except etcd.EtcdException as e:
                # Assume any other errors are fatal to our poll and
                # do a full resync.
                _log.exception("Unknown etcd error %r; doing resync.",
                               e.message)
                # Limit our retry rate in case etcd is down.
                time.sleep(1)
                self.reconnect()
                raise ResyncRequired()
            except:
                _log.exception("Unexpected exception during etcd poll")
                raise

        # Since we're polling on a subtree, we can't just increment
        # the index, we have to look at the modifiedIndex to spot
        # if we've skipped a lot of updates.
        self.next_etcd_index = max(self.next_etcd_index,
                                   response.modifiedIndex) + 1
        return response

    def stop(self):
        self._stopped = True

    def _on_pre_resync(self):
        """
        Abstract:

        Called before the initial dump is loaded and passed to
        _process_initial_dump().
        """
        pass

    def _on_snapshot_loaded(self, etcd_snapshot_response):
        """
        Abstract:

        Called once a snapshot has been loaded, replaces all previous
        state.

        Responsible for applying the snapshot.
        :param etcd_snapshot_response: Etcd response containing a complete dump.
        """
        pass


class StreamedEtcdResponse(object):
    def __init__(self, response):
        self.response = response

    @property
    def children(self):
        try:
            parser = ijson.parse(self.response)  # urllib3 response is file-like.
            stack = []
            frame = {}
            for prefix, event, value in parser:
                if event == "start_map":
                    stack.append(frame)
                    frame = {}
                elif event == "map_key":
                    frame["current_key"] = value
                elif event == "string":
                    frame[frame["current_key"]] = value
                    del frame["current_key"]
                    if "key" in frame and "value" in frame:
                        yield Node(frame["key"], frame["value"])
                elif event == "end_map":
                    frame = stack.pop(-1)
        except (urllib3.exceptions.HTTPError,
                HTTPException,
                socket.error) as e:
            _log.error("Request to etcd failed: %r", e)
            raise etcd.EtcdConnectionFailed(
                "Connection to etcd failed due to %r" % e, cause=e)

    @property
    def leaves(self):
        return self.children

class Node(object):
    __slots__ = ("key", "value", "action")

    def __init__(self, key, value, action=None):
        self.key = key
        self.value = value
        self.action = action


class ResyncRequired(Exception):
    pass
