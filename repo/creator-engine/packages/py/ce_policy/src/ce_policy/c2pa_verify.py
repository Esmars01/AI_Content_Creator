"""Classification of C2PA validation results (§27, `GET /v1/renders/{id}/verify`, render goldens).

The signer plugin reads the manifest (`provenance.verify`, c2pa-python lives only in the plugin,
I14); this module turns its validator codes into the verdicts dev, test and production need:

- `signature_valid`: the claim signature validated;
- `hashes_valid`: a content hash assertion (data, BMFF or boxes hash) matched and no hash failed;
- `trusted`: the signing credential chains to a trusted root (production certificates);
- `ok_untrusted_root`: signature and hashes valid and the only failure is the untrusted dev root —
  what dev and test expect from the generated test CA.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

__all__ = ["UNTRUSTED", "C2paVerdict", "classify_c2pa"]

_HASH_MATCH = frozenset({"assertion.dataHash.match", "assertion.bmffHash.match", "assertion.boxesHash.match"})
UNTRUSTED = "signingCredential.untrusted"


@dataclass(frozen=True)
class C2paVerdict:
    present: bool
    signature_valid: bool = False
    hashes_valid: bool = False
    trusted: bool = False
    failures: list[str] = field(default_factory=list)

    @property
    def ok_untrusted_root(self) -> bool:
        return self.present and self.signature_valid and self.hashes_valid and set(self.failures) <= {UNTRUSTED}

    @property
    def ok(self) -> bool:
        return self.ok_untrusted_root and self.trusted

    def as_dict(self) -> dict[str, Any]:
        return {
            "present": self.present,
            "signature_valid": self.signature_valid,
            "hashes_valid": self.hashes_valid,
            "trusted": self.trusted,
            "ok_untrusted_root": self.ok_untrusted_root,
            "ok": self.ok,
            "failures": self.failures,
        }


def classify_c2pa(present: bool, success: Iterable[str], failures: Iterable[str]) -> C2paVerdict:
    ok_codes = set(success)
    failed = sorted(set(failures))
    hash_failed = any("hash" in f.lower() or "mismatch" in f.lower() for f in failed)
    return C2paVerdict(
        present=present,
        signature_valid="claimSignature.validated" in ok_codes,
        hashes_valid=bool(ok_codes & _HASH_MATCH) and not hash_failed,
        trusted=present and UNTRUSTED not in failed,
        failures=failed,
    )
