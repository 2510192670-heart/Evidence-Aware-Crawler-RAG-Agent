"""Evaluation canonical JSON v1. Array order is semantic; object order is not."""
import hashlib
import json


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def source_digest(text):
    """Text source identity ignores checkout CRLF, never string values inside JSON."""
    return hashlib.sha256(text.replace('\r\n', '\n').encode('utf-8')).hexdigest()
