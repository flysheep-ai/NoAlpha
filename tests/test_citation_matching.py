"""Regression tests for the citation_support = 0.0 bug.

The LLM was shown evidence as `[E<hexid>]` but its returned evidence_ids were
validated against the bare `<hexid>` set, so every real citation was rejected
as fabricated. citation_support collapsed to 0.0 on every run and the fusion
layer permanently damped the LLM signal by 0.5.

These tests pin both halves of the contract:
  1. a reference the LLM copied out of the prompt must validate
  2. a reference the LLM invented must still be rejected
"""
from __future__ import annotations

import unittest

from noalpha.research.context_fields import ValidatedClaim
from noalpha.research.orchestrator import LLMResearchOrchestrator


def _claim(*refs: str) -> ValidatedClaim:
    return ValidatedClaim(
        claim_id="c1", text="revenue grew", claim_type="observation",
        evidence_ids=list(refs), inference_level="direct",
        confidence=0.8, caveats="", citation_status="pending",
    )


class CitationMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        # citation validation is pure; it never reaches the provider
        self.orch = LLMResearchOrchestrator(None)  # type: ignore[arg-type]

    def _status(self, valid_ids: set[str], *refs: str) -> str:
        return self.orch._validate_citations([_claim(*refs)], valid_ids)[0].citation_status

    def test_reference_copied_verbatim_from_prompt_is_supported(self):
        # the prompt renders evidence as [E<id>]; the LLM copies that whole token
        self.assertEqual(self._status({"a3f2c891deadbeef"}, "[Ea3f2c891deadbeef]"), "supported")

    def test_e_prefixed_reference_without_brackets_is_supported(self):
        self.assertEqual(self._status({"a3f2c891deadbeef"}, "Ea3f2c891deadbeef"), "supported")

    def test_bare_reference_is_supported(self):
        self.assertEqual(self._status({"a3f2c891deadbeef"}, "a3f2c891deadbeef"), "supported")

    def test_uppercased_reference_is_supported(self):
        self.assertEqual(self._status({"a3f2c891deadbeef"}, "A3F2C891DEADBEEF"), "supported")

    def test_hex_id_beginning_with_e_is_not_over_stripped(self):
        # evidence_id is sha256 hex, so it can legitimately start with 'e'.
        # Stripping a leading 'E' must never corrupt an already-correct id.
        self.assertEqual(self._status({"ea3f2c891deadbe1"}, "ea3f2c891deadbe1"), "supported")

    def test_fabricated_reference_is_still_unsupported(self):
        # the actual failure signature observed in the NVDA/TSLA E2E runs
        self.assertEqual(self._status({"a3f2c891deadbeef"}, "EV-Market-NVDA"), "unsupported")

    def test_claim_with_no_references_is_unsupported(self):
        self.assertEqual(self._status({"a3f2c891deadbeef"}), "unsupported")

    def test_mixed_real_and_fabricated_is_partially_supported(self):
        status = self._status({"a3f2c891deadbeef"}, "[Ea3f2c891deadbeef]", "EV-Made-Up")
        self.assertEqual(status, "partially_supported")

    def test_normalized_reference_is_stored_in_canonical_form(self):
        # downstream joins evidence by raw id, so the surviving ref must be canonical
        claim = self.orch._validate_citations(
            [_claim("[Ea3f2c891deadbeef]")], {"a3f2c891deadbeef"})[0]
        self.assertEqual(claim.evidence_ids, ["a3f2c891deadbeef"])


class ClaimsPromptTests(unittest.TestCase):
    def test_claims_prompt_lists_every_evidence_id(self):
        # the old prompt truncated evidence prose to 2000 chars, so the LLM was
        # asked for 8-15 claims while only seeing the first handful of ids and
        # had to invent the rest.
        ids = [f"{i:016x}" for i in range(40)]
        roster = LLMResearchOrchestrator._evidence_id_roster(ids)
        for evidence_id in ids:
            self.assertIn(evidence_id, roster)


if __name__ == "__main__":
    unittest.main()
