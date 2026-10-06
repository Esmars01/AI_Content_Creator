"""RunPod GPU providers (§25, Phase 9): `runpod_pod` (on-demand or spot pods) and `runpod_serverless`
(the minimum active workers of a serverless endpoint). Paid: they refuse to provision unless the
operator enabled paid provisioning (`allow_paid`) after the owner approved the spend (§41)."""
