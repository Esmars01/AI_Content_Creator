# plugins/provenance

Watermarking and signing.

Implemented (Phase 7): `c2pa/` — `c2pa_signer` (`provenance.sign`, `provenance.verify`) with c2pa-python; a throwaway dev CA in dev/test, operator certificates in production (ADR 0046).

Planned: `videoseal`, `audioseal` (Phase 8, `post` GPU family). In dev/test the watermark layers are the mock's `mock_dev` records — measured CPU cost in ADR 0047.
