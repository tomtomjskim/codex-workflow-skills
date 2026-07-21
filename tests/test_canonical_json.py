import unittest

from scripts.workflow_coordination.canonical_json import (
    CanonicalJSONError,
    canonical_bytes,
    load_canonical_input,
    sha256_id,
)


class CanonicalJSONTests(unittest.TestCase):
    def test_key_order_and_whitespace_have_same_hash(self):
        left = load_canonical_input(b'{"b":2, "a":1}')
        right = load_canonical_input(b'{\n"a":1,"b":2\n}')
        self.assertEqual(canonical_bytes(left), b'{"a":1,"b":2}')
        self.assertEqual(sha256_id(left), sha256_id(right))

    def test_rejects_duplicate_keys(self):
        with self.assertRaisesRegex(CanonicalJSONError, "duplicate key: a"):
            load_canonical_input(b'{"a":1,"a":2}')

    def test_rejects_floats_and_non_nfc_strings(self):
        with self.assertRaisesRegex(CanonicalJSONError, "floating-point"):
            load_canonical_input(b'{"value":1.5}')
        with self.assertRaisesRegex(CanonicalJSONError, "Unicode NFC"):
            canonical_bytes({"value": "e\u0301"})
