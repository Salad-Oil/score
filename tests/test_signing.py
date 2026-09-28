"""Signature exactness -- the highest-value test in the suite.

The exchange rebuilds the canonical string from the parameters *it* recognises
and compares the HMAC.  Two failure modes therefore matter more than any other
bug in this project:

* the canonical string is built in a different order (or with different
  formatting) than the server expects, and
* a parameter that is not part of the documented request sneaks into the signed
  string, which invalidates *every* request on that endpoint.

Both are pinned here against the official published vector, byte for byte.
"""

from __future__ import annotations

import unittest

from roostoo.client import canonical_params, sign_payload

# ---------------------------------------------------------------------------
# The official documentation vector.
# ---------------------------------------------------------------------------
DOC_SECRET = "S1XP1e3UZj6A7H5fATj0jNhqPxxdSJYdInClVN65XAbvqqMKjVHjA7PZj4W12oep"
DOC_CANONICAL = "pair=BNB/USD&quantity=2000&side=BUY&timestamp=1580774512000&type=MARKET"
DOC_SIGNATURE = "20b7fd5550b67b3bf0c1684ed0f04885261db8fdabd38611e9e6af23c19b7fff"

# The same five parameters, deliberately inserted in a hostile order.
DOC_PARAMS_SCRAMBLED = {
    "type": "MARKET",
    "timestamp": "1580774512000",
    "side": "BUY",
    "quantity": "2000",
    "pair": "BNB/USD",
}


class TestDocumentedVector(unittest.TestCase):
    """The published request must reproduce the published signature exactly."""

    def test_canonical_params_matches_documented_string(self) -> None:
        """canonical_params() reproduces the documented string byte-for-byte."""
        self.assertEqual(canonical_params(DOC_PARAMS_SCRAMBLED), DOC_CANONICAL)

    def test_sign_payload_matches_documented_digest(self) -> None:
        """sign_payload() reproduces the documented hex HMAC-SHA256 digest."""
        self.assertEqual(sign_payload(DOC_CANONICAL, DOC_SECRET), DOC_SIGNATURE)

    def test_documented_digest_is_lowercase_hex_sha256(self) -> None:
        """The digest is 64 lowercase hex characters -- the shape the API expects."""
        digest = sign_payload(DOC_CANONICAL, DOC_SECRET)
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, digest.lower())
        int(digest, 16)  # raises ValueError if it is not hex

    def test_end_to_end_scrambled_params_produce_documented_signature(self) -> None:
        """The whole path -- dict -> canonical -> HMAC -- matches the vector."""
        self.assertEqual(sign_payload(canonical_params(DOC_PARAMS_SCRAMBLED), DOC_SECRET), DOC_SIGNATURE)


class TestCanonicalOrdering(unittest.TestCase):
    """Key order must be a property of the *keys*, not of the caller."""

    def test_keys_sorted_lexicographically_regardless_of_insertion_order(self) -> None:
        """Two dicts with identical items but different insertion order agree."""
        ascending = {"a": 1, "b": 2, "c": 3, "z": 26}
        descending = {"z": 26, "c": 3, "b": 2, "a": 1}
        self.assertEqual(canonical_params(ascending), "a=1&b=2&c=3&z=26")
        self.assertEqual(canonical_params(ascending), canonical_params(descending))

    def test_uppercase_keys_sort_before_lowercase_keys(self) -> None:
        """Sorting is plain codepoint order -- 'Z' < 'a', and that is what we send."""
        self.assertEqual(canonical_params({"a": 1, "Z": 2}), "Z=2&a=1")

    def test_values_are_stringified_without_reformatting(self) -> None:
        """Values pass through f-string interpolation untouched (no float noise)."""
        self.assertEqual(canonical_params({"quantity": "2000", "price": "0.30"}), "price=0.30&quantity=2000")

    def test_empty_params_give_empty_string(self) -> None:
        """An unsigned endpoint with no parameters signs the empty string."""
        self.assertEqual(canonical_params({}), "")

    def test_params_dict_is_not_mutated(self) -> None:
        """canonical_params() is pure: the caller's dict keeps its own order."""
        params = {"z": 1, "a": 2}
        canonical_params(params)
        self.assertEqual(list(params), ["z", "a"])


class TestDigestSensitivity(unittest.TestCase):
    """Every meaningful change to the request must change the signature."""

    def test_changed_parameter_value_changes_digest(self) -> None:
        """A one-digit quantity change produces a completely different digest."""
        tampered = dict(DOC_PARAMS_SCRAMBLED, quantity="2001")
        self.assertNotEqual(sign_payload(canonical_params(tampered), DOC_SECRET), DOC_SIGNATURE)

    def test_changed_side_changes_digest(self) -> None:
        """BUY -> SELL must not reuse the same signature."""
        tampered = dict(DOC_PARAMS_SCRAMBLED, side="SELL")
        self.assertNotEqual(sign_payload(canonical_params(tampered), DOC_SECRET), DOC_SIGNATURE)

    def test_extra_parameter_changes_digest(self) -> None:
        """An undocumented extra field invalidates the signature.

        The server rebuilds the string from the parameters it recognises, so an
        extra field such as ``price`` on a MARKET order (or ``note``, or
        ``recv_window``) makes the two strings differ and the request is
        rejected.  This is the "signature must contain exactly the documented
        params" hazard.
        """
        with_extra = dict(DOC_PARAMS_SCRAMBLED, price="0.30")
        self.assertNotEqual(sign_payload(canonical_params(with_extra), DOC_SECRET), DOC_SIGNATURE)

    def test_missing_parameter_changes_digest(self) -> None:
        """Dropping a documented field also breaks the match."""
        without_timestamp = {k: v for k, v in DOC_PARAMS_SCRAMBLED.items() if k != "timestamp"}
        self.assertNotEqual(sign_payload(canonical_params(without_timestamp), DOC_SECRET), DOC_SIGNATURE)

    def test_changed_secret_changes_digest(self) -> None:
        """The secret is the key: a different account cannot forge the digest."""
        self.assertNotEqual(sign_payload(DOC_CANONICAL, DOC_SECRET + "x"), DOC_SIGNATURE)

    def test_signature_is_deterministic(self) -> None:
        """Signing the same payload twice is stable (no salt, no nonce)."""
        self.assertEqual(sign_payload(DOC_CANONICAL, DOC_SECRET), sign_payload(DOC_CANONICAL, DOC_SECRET))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
