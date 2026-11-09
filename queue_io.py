"""Serialize queue appends and prefix acknowledgements across server/wrapper processes."""
from contextlib import contextmanager
import hashlib
import json
import os
import time


@contextmanager
def queue_lock(path):
    # A separate, stable inode also covers a queue being created for the first time.
    with open(path.with_suffix('.lock'), 'a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0)
            if not lock.read(1):
                lock.write(b'\0')
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def append_entry(path, entry):
    with queue_lock(path):
        with open(path, 'a', encoding='utf-8') as queue:
            queue.write(json.dumps(entry) + '\n')


def read_batch(path):
    """Leave the snapshot queued until its delivery has a disposition."""
    with queue_lock(path):
        try:
            with open(path, 'r+', encoding='utf-8') as queue:
                lines = queue.readlines()
                journal = path.with_suffix('.attempt.json')
                if journal.exists():
                    with open(journal, encoding='utf-8') as record:
                        attempt = json.load(record)
                    prefix = lines[:attempt['lines']]
                    if _digest(prefix) == attempt['sha256']:
                        # The previous wrapper died during injection/acknowledgement.
                        # A paste may have arrived: preserve it for manual recovery.
                        _finish_locked(path, queue, prefix, 'uncertain', attempt['attempts'])
                        queue.seek(0)
                        lines = queue.readlines()
                    else:
                        # The journalled prefix is no longer present; do not replay it.
                        journal.unlink()
        except FileNotFoundError:
            return []
    # An older writer may not yet have finished its last line.
    if lines and not lines[-1].endswith('\n'):
        lines.pop()
    return lines


def _digest(lines):
    return hashlib.sha256(''.join(lines).encode()).hexdigest()


def begin_batch(path, lines, attempts):
    """Persist the ambiguous boundary before attempting any terminal write."""
    with queue_lock(path):
        journal = path.with_suffix('.attempt.json')
        tmp = path.with_suffix('.attempt.tmp')
        with open(tmp, 'w', encoding='utf-8') as record:
            json.dump({'sha256':_digest(lines), 'lines':len(lines), 'attempts':attempts}, record)
            record.flush()
            os.fsync(record.fileno())
        os.replace(tmp, journal)


def _finish_locked(path, queue, lines, status, attempts, consume=True, reason=None, identity_id=None):
    prefix = ''.join(lines)
    queue.seek(0)
    current = queue.read()
    matches = current.startswith(prefix)
    record = {'time':time.time(), 'outcome':status if matches else 'prefix-changed', 'attempts':attempts,
              'lines':len(lines), 'sha256':_digest(lines)}
    if not matches:
        record['delivery_outcome'] = status
    if reason:
        record['reason'] = reason
    if identity_id:
        record['identity_id'] = identity_id   # the consuming instance (ad-e5gz migration check)
    with open(path.with_suffix('.delivery.jsonl'), 'a', encoding='utf-8') as trace:
        trace.write(json.dumps(record) + '\n')
        trace.flush()
        os.fsync(trace.fileno())
    if status in ('uncertain', 'not-pasted-exhausted'):
        with open(path.with_suffix('.failed.jsonl'), 'a', encoding='utf-8') as failed:
            failed.write(prefix)
            failed.flush()
            os.fsync(failed.fileno())
    if consume and matches:
        queue.seek(0)
        queue.write(current[len(prefix):])
        queue.truncate()
        queue.flush()
        os.fsync(queue.fileno())
    path.with_suffix('.attempt.json').unlink(missing_ok=True)
    return matches


def retain_stale(path, lines):
    """Keep wakes addressed to another identity for manual recovery. Idempotent: a retry re-reads them."""
    with queue_lock(path):
        failed = path.with_suffix('.failed.jsonl')
        try:
            known = set(failed.read_text(encoding='utf-8').splitlines(keepends=True))
        except FileNotFoundError:
            known = set()
        fresh = [line for line in lines if line not in known]
        if not fresh:
            return
        with open(failed, 'a', encoding='utf-8') as record:
            record.write(''.join(fresh))
            record.flush()
            os.fsync(record.fileno())
        with open(path.with_suffix('.delivery.jsonl'), 'a', encoding='utf-8') as trace:
            trace.write(json.dumps({'time':time.time(), 'outcome':'stale-identity', 'attempts':0,
                                    'lines':len(fresh), 'sha256':_digest(fresh)}) + '\n')
            trace.flush()
            os.fsync(trace.fileno())


def finish_batch(path, lines, status, attempts, *, consume=True, reason=None, identity_id=None):
    """Keep concurrent appends; retain uncertain/exhausted deliveries for manual recovery.

    The trace contains hashes/counts rather than prompt text, plus `reason`, a fixed label naming
    why a delivery ended uncertain (ad-ruq), when the injector gave one, and `identity_id`, the
    consuming wrapper's own identity, when it has one (ad-e5gz). Only a proven pre-paste
    refusal is retryable. An ambiguous result is preserved, never automatically resent.
    False means another consumer changed the prefix; the caller must read a fresh batch.
    """
    with queue_lock(path):
        with open(path, 'r+', encoding='utf-8') as queue:
            return _finish_locked(path, queue, lines, status, attempts, consume, reason, identity_id)
