"""Classification of C2PA validator codes (§27): dev expects a valid signature and hashes with an
untrusted root; production also needs trust."""

from __future__ import annotations

from ce_policy.c2pa_verify import classify_c2pa

SUCCESS = ["claimSignature.validated", "assertion.bmffHash.match", "assertion.hashedURI.match"]


def test_untrusted_dev_root_is_ok_for_dev_only() -> None:
    verdict = classify_c2pa(True, SUCCESS, ["signingCredential.untrusted"])
    assert verdict.signature_valid and verdict.hashes_valid and not verdict.trusted
    assert verdict.ok_untrusted_root and not verdict.ok


def test_trusted_and_broken_manifests() -> None:
    assert classify_c2pa(True, SUCCESS, []).ok
    tampered = classify_c2pa(True, ["claimSignature.validated"], ["assertion.bmffHash.mismatch"])
    assert not tampered.hashes_valid and not tampered.ok_untrusted_root
    assert not classify_c2pa(False, [], []).ok_untrusted_root
