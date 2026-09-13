"""Access to the frozen dev / held-out benchmark split.

``tasks.json`` is the M4.1 answer key over the loopback fixtures in
``fixtures/``. Its canonical content hash is recorded in ``tasks.sha256`` so
that any later edit to a task is detected: changing the split requires a
deliberate update of both files.
"""

import hashlib
import json
from pathlib import Path

MANIFEST_PATH = Path(__file__).with_name('tasks.json')
HASH_PATH = Path(__file__).with_name('tasks.sha256')

SCENARIOS = ('S1', 'S2', 'S3', 'S4', 'S5')
SPLITS = ('dev', 'held_out')


def load():
    return json.loads(MANIFEST_PATH.read_text(encoding='utf-8'))


def canonical_bytes(manifest):
    """Stable serialization; formatting in ``tasks.json`` must not change the hash."""
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def content_sha256(manifest=None):
    return hashlib.sha256(canonical_bytes(load() if manifest is None else manifest)).hexdigest()
