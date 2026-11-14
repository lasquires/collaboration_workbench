import hashlib
import unittest

from tools.workspace_bundle_chunks import (
    ChunkError, DEFAULT_CHUNK_BYTES, DEFAULT_MAX_BYTES, FORMAT, MAX_CHUNKS,
    canonical, pack, unpack, validate_manifest,
)


class BundleChunkTests(unittest.TestCase):
    def test_round_trip_splits_inside_unicode_codepoint(self):
        payload = {'text': '🙂' * 20, 'nested': [1, True, None]}
        manifest, chunks = pack(payload, chunk_bytes=7)
        self.assertEqual(manifest['format'], FORMAT)
        self.assertEqual(manifest['payload_bytes'], len(canonical(payload)))
        self.assertTrue(any(chunk[-1] & 0xC0 == 0x80 for chunk in chunks.values()))
        self.assertEqual(unpack(manifest, chunks.__getitem__), payload)

    def test_manifest_exact_shape_and_bounds(self):
        self.assertEqual(DEFAULT_CHUNK_BYTES, 4 * 1024 * 1024)
        self.assertEqual(DEFAULT_MAX_BYTES, 256 * 1024 * 1024)
        self.assertEqual(MAX_CHUNKS, 1024)
        manifest, _ = pack({'x': 'a' * 99}, chunk_bytes=10)
        for mutate in (
            lambda m: m.update(extra=True),
            lambda m: m.update(payload_bytes=True),
            lambda m: m['chunks'][0].update(byte_length=True),
            lambda m: m.update(chunk_bytes=0),
            lambda m: m.update(payload_bytes=m['payload_bytes'] + 1),
        ):
            changed = __import__('copy').deepcopy(manifest)
            mutate(changed)
            with self.subTest(changed=changed), self.assertRaises(ChunkError):
                validate_manifest(changed, max_chunk_bytes=10)

    def test_missing_substituted_truncated_and_reordered_chunks_fail(self):
        manifest, chunks = pack({'text': 'unicode🙂' * 30}, chunk_bytes=11)
        with self.assertRaises(ChunkError):
            unpack(manifest, lambda digest: (_ for _ in ()).throw(KeyError(digest)))
        bad = dict(chunks)
        first_hash = manifest['chunks'][0]['sha256']
        bad[first_hash] = b'X' * len(bad[first_hash])
        with self.assertRaises(ChunkError):
            unpack(manifest, bad.__getitem__)
        reverse = dict(manifest, chunks=list(reversed(manifest['chunks'])))
        with self.assertRaises(ChunkError):
            unpack(reverse, chunks.__getitem__)
        truncated = dict(chunks)
        truncated[first_hash] = truncated[first_hash][:-1]
        with self.assertRaises(ChunkError):
            unpack(manifest, truncated.__getitem__)

    def test_aggregate_piece_and_object_limits(self):
        with self.assertRaises(ChunkError):
            canonical({'x': 'a' * 100}, max_total_bytes=10)
        with self.assertRaises(ChunkError):
            pack({'x': 'a' * 100}, chunk_bytes=10, max_total_bytes=20)
        manifest, chunks = pack({'x': 'a' * 100}, chunk_bytes=10)
        with self.assertRaises(ChunkError):
            validate_manifest(manifest, max_total_bytes=20, max_chunk_bytes=10)
        too_many = dict(manifest, chunks=[{'sha256': hashlib.sha256(b'x').hexdigest(), 'byte_length': 10}] * 1025)
        with self.assertRaises(ChunkError):
            validate_manifest(too_many, max_chunk_bytes=10)

    def test_empty_object_has_one_nonempty_chunk(self):
        manifest, chunks = pack({}, chunk_bytes=2)
        self.assertEqual(unpack(manifest, chunks.__getitem__), {})
        self.assertEqual(len(manifest['chunks']), 1)


if __name__ == '__main__':
    unittest.main()
