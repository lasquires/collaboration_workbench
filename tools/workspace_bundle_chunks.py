"""Canonical JSON payloads split into bounded, content-addressed byte chunks."""
from __future__ import annotations

import hashlib
import json
import re

FORMAT = 'workbench-chunked-json'
VERSION = 1
DEFAULT_CHUNK_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
MAX_CHUNKS = 1024
_HEX = re.compile(r'^[a-f0-9]{64}$')


class ChunkError(ValueError):
    """Invalid or incomplete chunked JSON payload."""


def canonical(payload, max_total_bytes=DEFAULT_MAX_BYTES) -> bytes:
    if not isinstance(payload, dict):
        raise ChunkError('Payload must be a JSON object')
    if type(max_total_bytes) is not int or max_total_bytes < 2:
        raise ChunkError('Invalid aggregate byte bound')
    try:
        raw = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ChunkError('Payload is not canonical JSON') from None
    if len(raw) > max_total_bytes:
        raise ChunkError('Payload exceeds aggregate byte bound')
    return raw


def pack(payload, *, chunk_bytes=DEFAULT_CHUNK_BYTES, max_total_bytes=DEFAULT_MAX_BYTES):
    if type(chunk_bytes) is not int or chunk_bytes < 1:
        raise ChunkError('Invalid chunk byte bound')
    raw = canonical(payload, max_total_bytes)
    pieces = [raw[i:i + chunk_bytes] for i in range(0, len(raw), chunk_bytes)]
    if not pieces:
        raise ChunkError('Canonical payload unexpectedly empty')
    if len(pieces) > MAX_CHUNKS:
        raise ChunkError('Too many chunks')
    refs, blobs = [], {}
    for piece in pieces:
        digest = hashlib.sha256(piece).hexdigest()
        refs.append({'sha256': digest, 'byte_length': len(piece)})
        blobs[digest] = piece
    manifest = {'format': FORMAT, 'version': VERSION, 'payload_bytes': len(raw),
                'payload_sha256': hashlib.sha256(raw).hexdigest(),
                'chunk_bytes': chunk_bytes, 'chunks': refs}
    return manifest, blobs


def validate_manifest(manifest, *, max_total_bytes=DEFAULT_MAX_BYTES,
                      max_chunk_bytes=DEFAULT_CHUNK_BYTES) -> None:
    fields = {'format', 'version', 'payload_bytes', 'payload_sha256', 'chunk_bytes', 'chunks'}
    if not isinstance(manifest, dict) or set(manifest) != fields:
        raise ChunkError('Manifest fields are invalid')
    if manifest['format'] != FORMAT or type(manifest['version']) is not int or manifest['version'] != VERSION:
        raise ChunkError('Manifest format or version is invalid')
    total, size = manifest['payload_bytes'], manifest['chunk_bytes']
    if type(max_total_bytes) is not int or type(max_chunk_bytes) is not int or max_total_bytes < 2 or max_chunk_bytes < 1:
        raise ChunkError('Invalid configured bounds')
    if type(total) is not int or total < 2 or total > max_total_bytes:
        raise ChunkError('Payload size is invalid')
    if type(size) is not int or size < 1 or size > max_chunk_bytes:
        raise ChunkError('Chunk size is invalid')
    if not isinstance(manifest['payload_sha256'], str) or not _HEX.fullmatch(manifest['payload_sha256']):
        raise ChunkError('Payload digest is invalid')
    refs = manifest['chunks']
    if not isinstance(refs, list) or not refs or len(refs) > MAX_CHUNKS:
        raise ChunkError('Chunk list is invalid')
    summed = 0
    seen = {}
    for i, ref in enumerate(refs):
        if not isinstance(ref, dict) or set(ref) != {'sha256', 'byte_length'}:
            raise ChunkError('Chunk reference fields are invalid')
        digest, length = ref['sha256'], ref['byte_length']
        if not isinstance(digest, str) or not _HEX.fullmatch(digest) or type(length) is not int:
            raise ChunkError('Chunk reference is invalid')
        expected = size if i < len(refs) - 1 else total - size * (len(refs) - 1)
        if length <= 0 or length > size or length != expected:
            raise ChunkError('Chunk lengths or order are invalid')
        if digest in seen and seen[digest] != length:
            raise ChunkError('Duplicate digest has inconsistent length')
        seen[digest] = length
        summed += length
    if summed != total or len(refs) != (total + size - 1) // size:
        raise ChunkError('Chunk lengths do not match payload size')


def unpack(manifest, read_chunk, *, max_total_bytes=DEFAULT_MAX_BYTES,
           max_chunk_bytes=DEFAULT_CHUNK_BYTES) -> dict:
    validate_manifest(manifest, max_total_bytes=max_total_bytes, max_chunk_bytes=max_chunk_bytes)
    raw_parts = []
    for ref in manifest['chunks']:
        try:
            piece = read_chunk(ref['sha256'])
        except Exception:
            raise ChunkError('Chunk is missing') from None
        if not isinstance(piece, bytes) or len(piece) != ref['byte_length']:
            raise ChunkError('Chunk length is invalid')
        if hashlib.sha256(piece).hexdigest() != ref['sha256']:
            raise ChunkError('Chunk digest is invalid')
        raw_parts.append(piece)
    raw = b''.join(raw_parts)
    if len(raw) != manifest['payload_bytes'] or hashlib.sha256(raw).hexdigest() != manifest['payload_sha256']:
        raise ChunkError('Payload digest is invalid')
    try:
        payload = json.loads(raw.decode('utf-8'))
        if canonical(payload, max_total_bytes) != raw:
            raise ChunkError('Payload is not canonical JSON')
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ChunkError('Payload JSON is invalid') from None
    return payload
