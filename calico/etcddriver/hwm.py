# -*- coding: utf-8 -*-
# Copyright 2015 Metaswitch Networks
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
calico.etcddriver.hwm
~~~~~~~~~~~~~~~~~~~~~

The HighWaterTracker is used to resolve the high water mark for each etcd
key when processing a snapshot and event stream in parallel.
"""

import logging
import re
import string

from datrie import Trie
import datrie

_log = logging.getLogger(__name__)


TRIE_CHARS = string.ascii_letters + string.digits + "/_-"
TRIE_CHARS_MATCH = re.compile(r'^[%s]+$' % re.escape(TRIE_CHARS))


class HighWaterTracker(object):
    """
    Tracks the highest etcd index for which we've seen a particular
    etcd key.
    """
    def __init__(self):
        self._hwms = Trie(TRIE_CHARS)

        # Set to a Trie while we're tracking deletions.  None otherwise.
        self._deletion_hwms = None
        self._latest_deletion = None

    def start_tracking_deletions(self):
        """
        Starts tracking which subtrees have been deleted so that update_hwm
        can skip updates to keys that have subsequently been deleted.

        Should be paired with a call to stop_tracking_deletions() to release
        the associated tracking data structures.
        """
        _log.info("Started tracking deletions")
        self._deletion_hwms = Trie(TRIE_CHARS)
        self._latest_deletion = None

    def stop_tracking_deletions(self):
        """
        Stops deletion tracking and frees up the associated resources.

        Calling this asserts that subsequent calls to update_hwm() will only
        use HWMs after any stored deletes.
        """
        _log.info("Stopped tracking deletions")
        self._deletion_hwms = None
        self._latest_deletion = None

    def update_hwm(self, key, hwm):
        """
        Updates the HWM for a key if the new value is greater than the old.
        If deletion tracking is enabled, resolves deletions so that updates
        to subtrees that have been deleted are skipped iff the deletion is
        after the update in HWM order.

        :return int|NoneType: the old HWM of the key (or the HWM at which it
                was deleted) or None if it did not previously exist.
        """
        _log.debug("Updating HWM for %s to %s", key, hwm)
        key = encode_key(key)
        if (self._deletion_hwms is not None and
                # Optimization: avoid expensive lookup if this update comes
                # after all deletions.
                hwm < self._latest_deletion):
            # We're tracking deletions, check that this key hasn't been
            # deleted.
            del_hwm = self._deletion_hwms.longest_prefix_value(key, None)
            if del_hwm > hwm:
                _log.debug("Key %s previously deleted, skipping", key)
                return del_hwm
        try:
            old_hwm = self._hwms[key]  # Trie doesn't have get().
        except KeyError:
            old_hwm = None
        if old_hwm < hwm:  # Works for None too.
            _log.debug("Key %s HWM updated to %s, previous %s",
                       key, hwm, old_hwm)
            self._hwms[key] = hwm
        return old_hwm

    def store_deletion(self, key, hwm):
        """
        Store that a given key (or directory) was deleted at a given HWM.
        :return: List of known keys that were deleted.  This will be the
                 leaves only when a subtree is being deleted.
        """
        _log.debug("Key %s deleted", key)
        key = encode_key(key)
        self._latest_deletion = max(hwm, self._latest_deletion)
        if self._deletion_hwms is not None:
            _log.debug("Tracking deletion in deletions trie")
            self._deletion_hwms[key] = hwm
        deleted_keys = []
        for child_key, child_mod in self._hwms.items(key):
            del self._hwms[child_key]
            deleted_keys.append(decode_key(child_key))
        _log.debug("Found %s keys deleted under %s", len(deleted_keys), key)
        return deleted_keys

    def remove_old_keys(self, hwm_limit):
        """
        Deletes and returns all keys that have HWMs less than hwm_limit.
        :return: list of keys that were deleted.
        """
        assert not self._deletion_hwms, \
            "Delete tracking incompatible with remove_old_keys()"
        _log.info("Removing keys that are older than %s", hwm_limit)
        old_keys = []
        state = datrie.State(self._hwms)
        state.walk(u"")
        it = datrie.Iterator(state)
        while it.next():
            value = it.data()
            if value < hwm_limit:
                old_keys.append(it.key())
        for old_key in old_keys:
            del self._hwms[old_key]
        _log.info("Deleted %s old keys", len(old_keys))
        return map(decode_key, old_keys)


def encode_key(key):
    # FIXME May have to be more lenient
    assert TRIE_CHARS_MATCH.match(key)
    if key[-1] != "/":
        key += "/"
    return key


def decode_key(key):
    return key[:-1]
