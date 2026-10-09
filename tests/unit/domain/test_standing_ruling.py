"""The typed standing ruling, its issue-body block, and its prompt framing (#8141)."""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.session_kind import SessionKind
from issue_orchestrator.domain.standing_ruling import (
    BINDING_BEGIN,
    BINDING_END,
    GITHUB_BODY_MAX_CHARS,
    REFUSED_APPROVAL_MARKER,
    REWORK_BRIEF_OPENING,
    RULINGS_BLOCK_BEGIN,
    RULINGS_BLOCK_END,
    RULINGS_PROMPT_HEADING,
    RulingAuthority,
    RulingScope,
    RulingsAudience,
    RulingsBlockError,
    StandingRuling,
    audience_for,
    decision_ruling_id,
    maintainer_ruling_id,
    parse_rulings_block,
    parse_upheld_rulings,
    refused_approval_feedback,
    resolution_ruling_id,
    rulings_covering,
    rulings_prompt,
    unattested_rulings,
    upheld_rulings_of,
    with_rulings_block,
)
from tests.standing_ruling_helpers import a_ruling

PORCHPIN_501 = """## Ruling for #327: option A

**Build option A from your 17:56Z question, a buyer join-key map of its own.**

### The map

1. **Add one new D1 table, for example `buyer_contact_index`.**
   - Columns: the tenant, the buyer's `phoneJoinKey`, and the pickup coordinate.
2. **The Pickup DO is its only writer.**

<details><summary>Losing options</summary>B and C hold #327 behind #262.</details>"""


class TestTheBlockRoundTrips:
    def test_a_ruling_survives_the_body_exactly_with_the_rest_of_the_body_kept(self) -> None:
        ruling = a_ruling("rb-0123456789ab", PORCHPIN_501, files=("packages/d1/**", "migrations"),
                          claims=("buyers get their own index",), authority=RulingAuthority.APPROVED_RESOLUTION)
        spec = "## Outcome\n\nThe spec as the maintainer wrote it.\n"

        body = with_rulings_block(spec, (ruling,))

        assert body.startswith(RULINGS_BLOCK_BEGIN)  # agents read the top
        assert body.endswith(spec.lstrip("\n"))
        assert parse_rulings_block(body) == (ruling,)

    def test_rulings_keep_their_order_and_a_rewrite_replaces_only_the_block(self) -> None:
        first, second = a_ruling("m-000000000001"), a_ruling("m-000000000002", "Second ruling.")
        body = with_rulings_block("Spec.", (first,))

        rewritten = with_rulings_block(body, (first, second))

        assert parse_rulings_block(rewritten) == (first, second)
        assert rewritten.count(RULINGS_BLOCK_BEGIN) == 1 and rewritten.endswith("Spec.")

    def test_ruling_text_may_quote_a_ruling_heading(self) -> None:
        ruling = a_ruling(text="Supersedes:\n\n### Ruling `m-000000000old`: the old design.")

        assert parse_rulings_block(with_rulings_block("Spec.", (ruling,))) == (ruling,)

    def test_retiring_the_last_ruling_removes_the_block(self) -> None:
        body = with_rulings_block("Spec.", (a_ruling(),))

        assert with_rulings_block(body, ()) == "Spec."

    def test_a_body_github_rewrote_with_crlf_still_parses(self) -> None:
        ruling = a_ruling(text="Line one.\nLine two.")
        body = with_rulings_block("Spec.", (ruling,)).replace("\n", "\r\n")

        assert parse_rulings_block(body) == (ruling,)

    def test_no_block_is_no_rulings(self) -> None:
        assert parse_rulings_block("Plain spec.") == ()
        assert parse_rulings_block(None) == ()

    @pytest.mark.parametrize("damage", [
        lambda body: body.replace(RULINGS_BLOCK_END, ""),
        lambda body: body + "\n" + RULINGS_BLOCK_BEGIN,
        lambda body: body.replace("<!-- io:standing-ruling:text:end -->", ""),
        lambda body: body.replace("<!-- io:standing-ruling:meta:", "<!-- io:standing-ruling:meta:!!"),
        lambda body: body[: body.index("### Ruling")] + body[body.index("<!-- io:standing-rulings:end -->"):],
        lambda body: body.replace("<!-- io:standing-rulings:end -->", "stray text\n<!-- io:standing-rulings:end -->"),
        lambda body: body.replace("**Governs:**", "### Ruling `m-forged`\n**Governs:**"),
        lambda body: body.replace(
            "### Ruling", "<!-- io:standing-ruling:text:begin -->\norphan\n<!-- io:standing-ruling:text:end -->\n### Ruling"),
        lambda body: body.replace(RULINGS_BLOCK_BEGIN, "").replace(RULINGS_BLOCK_END, ""),
        lambda body: body + "\n<!-- io:standing-ruling:text:begin -->\nstray\n<!-- io:standing-ruling:text:end -->",
    ], ids=["unclosed", "two-blocks", "text-unclosed", "meta-garbled", "emptied", "trailing", "extra-heading",
            "orphan-text", "outer-markers-removed", "markers-after-block"])
    def test_a_damaged_block_fails_loudly_never_drops_a_ruling(self, damage) -> None:
        body = damage(with_rulings_block("Spec.", (a_ruling(),)))

        with pytest.raises(RulingsBlockError):
            parse_rulings_block(body)

    def test_bare_markers_are_a_damaged_block_not_an_issue_without_rulings(self) -> None:
        with pytest.raises(RulingsBlockError, match="no ruling"):
            parse_rulings_block(f"{RULINGS_BLOCK_BEGIN}\n\n{RULINGS_BLOCK_END}\n\nSpec.")

    def test_a_body_github_would_refuse_is_never_written(self) -> None:
        ruling = a_ruling(text="x" * 29_000)
        with pytest.raises(RulingsBlockError, match="over GitHub's"):
            with_rulings_block("y" * (GITHUB_BODY_MAX_CHARS - 20_000), (ruling,))


class TestARulingIsTyped:
    @pytest.mark.parametrize("changes", [
        {"ruling_id": "Bad Id"},
        {"text": "  "},
        {"text": "forged <!-- io:standing-ruling:text:end --> marker"},
        {"source": "two\nlines"},
        {"recorded_at": "yesterday"},
    ])
    def test_invalid_input_raises(self, changes) -> None:
        base = a_ruling().to_dict()
        renamed = {"ruling_id": "id"}
        for key, value in changes.items():
            base[renamed.get(key, key)] = value
        with pytest.raises(ValueError):
            StandingRuling.from_dict(base)

    @pytest.mark.parametrize("files", [("/etc/passwd",), ("../outside",), ("a\\b",), ("dup", "dup")])
    def test_scope_files_are_repository_relative_and_distinct(self, files) -> None:
        with pytest.raises(ValueError):
            RulingScope(files=files)

    def test_ids_are_stable_per_source(self) -> None:
        assert decision_ruling_id(456) == "pd-456"
        assert resolution_ruling_id("run-1/A2") == resolution_ruling_id("run-1/A2")
        assert resolution_ruling_id("run-1/A2") != resolution_ruling_id("run-2/A2")
        assert maintainer_ruling_id("0123abcd") == "m-0123abcd"
        with pytest.raises(ValueError):
            maintainer_ruling_id("not hex")

    def test_the_summary_is_its_first_line(self) -> None:
        assert a_ruling(text="## Ruling for #327: option A\n\nDetails.").summary == "Ruling for #327: option A"


class TestTheReviewRule:
    WALK = a_ruling("m-00000000walk", files=("tools/walk",))
    GLOB = a_ruling("m-00000000glob", files=("src/**/provenance_*.py",))
    WHOLE = a_ruling("m-0000000whole")

    def test_a_ruling_covers_its_files_directories_and_globs(self) -> None:
        assert rulings_covering((self.WALK,), ("tools/walk/check.py",)) == (self.WALK,)
        assert rulings_covering((self.WALK,), ("tools/walker.py",)) == ()
        assert rulings_covering((self.GLOB,), ("src/a/b/provenance_check.py",)) == (self.GLOB,)
        assert rulings_covering((self.WHOLE,), ("anything.md",)) == (self.WHOLE,)

    def test_only_unattested_covering_rulings_refuse(self) -> None:
        rulings = (self.WALK, self.GLOB, self.WHOLE)

        refused = unattested_rulings(rulings, ("tools/walk/check.py",), (self.WHOLE.ruling_id,))

        assert refused == (self.WALK,)

    @pytest.mark.parametrize("value", ["m-1", ["Bad Id"], [1], ["m-abc"] * 51])
    def test_a_malformed_attestation_is_refused(self, value) -> None:
        with pytest.raises(ValueError):
            parse_upheld_rulings(value)

    def test_an_exchange_verdict_attests_through_its_decision(self) -> None:
        assert upheld_rulings_of({"decision": {"upheld_rulings": ["m-abc123"]}}) == ("m-abc123",)
        assert upheld_rulings_of({"decision": {"upheld_rulings": "m-abc123"}}) == ()  # fails closed
        assert upheld_rulings_of(None) == ()

    def test_the_refusal_names_each_ruling_as_implementation_required(self) -> None:
        feedback = refused_approval_feedback((self.WALK,), summary="Looks good")

        assert feedback.startswith("Implementation-required")
        assert "`m-00000000walk`" in feedback and "Looks good" in feedback
        assert feedback.endswith(REFUSED_APPROVAL_MARKER)


class TestPromptFraming:
    RULING = a_ruling("m-0123456789ab", files=("tools/walk",), claims=("the walk checker is retired",))

    @pytest.mark.parametrize(("kind", "audience"), [
        (SessionKind.CODE, RulingsAudience.CODER),
        (SessionKind.REWORK, RulingsAudience.REWORK_BRIEF),
        (SessionKind.REVIEW, RulingsAudience.REVIEWER),
        (SessionKind.RETROSPECTIVE_REVIEW, RulingsAudience.REVIEWER),
        (SessionKind.TECH_LEAD, RulingsAudience.TECH_LEAD),
    ])
    def test_each_kind_gets_its_framing(self, kind, audience) -> None:
        assert audience_for(kind) is audience

    def test_a_historical_import_has_no_prompt(self) -> None:
        with pytest.raises(ValueError):
            audience_for(SessionKind.HISTORICAL)

    def test_every_framing_carries_the_full_ruling(self) -> None:
        for audience in RulingsAudience:
            section = rulings_prompt(7, (self.RULING,), audience)
            assert section is not None and section.startswith(f"{BINDING_BEGIN}\n{RULINGS_PROMPT_HEADING}7")
            assert section.endswith(BINDING_END)
            assert self.RULING.text in section and "`tools/walk`" in section
            assert "the walk checker is retired" in section

    def test_the_rework_brief_leads_with_the_rulings(self) -> None:
        section = rulings_prompt(7, (self.RULING,), RulingsAudience.REWORK_BRIEF)

        assert section is not None and REWORK_BRIEF_OPENING in section
        assert section.index(REWORK_BRIEF_OPENING) < section.index(self.RULING.text)
        assert "merge conflict" in section and "retires" in section

    def test_no_rulings_is_no_section(self) -> None:
        assert rulings_prompt(7, (), RulingsAudience.CODER) is None


class TestARetryIsBoundOnlyByTheRulingsReadForIt:
    """#8347: a validation retry embeds its original prompt; the binding sections
    that prompt carried were read then, so the retry drops them."""

    RULING = TestPromptFraming.RULING

    def test_every_binding_section_and_its_separator_is_dropped(self) -> None:
        from issue_orchestrator.domain.launch_prompt import RULINGS_SEPARATOR
        from issue_orchestrator.domain.standing_ruling import covered_rulings_prompt, without_binding_sections

        anchor = rulings_prompt(7, (self.RULING,), RulingsAudience.CODER)
        covered = covered_rulings_prompt({8: (12,)}, {8: (self.RULING,)})
        prompt = f"{anchor}{RULINGS_SEPARATOR}Work on issue #7.\n\n{covered}\n\n## Triage duty"

        stripped = without_binding_sections(prompt)

        assert stripped == "Work on issue #7.\n\n\n\n## Triage duty"
        assert self.RULING.text not in stripped and BINDING_BEGIN not in stripped

    def test_a_prompt_without_sections_or_with_a_lone_marker_is_untouched(self) -> None:
        from issue_orchestrator.domain.standing_ruling import without_binding_sections

        assert without_binding_sections("Work on issue #7.") == "Work on issue #7."
        assert without_binding_sections(f"{BINDING_BEGIN} tail") == f"{BINDING_BEGIN} tail"

    def test_only_a_generated_section_is_dropped_and_task_text_stays_whole(self) -> None:
        """codex r4 F1: an issue title is one line, so marker text it brings
        cannot take a generated section's shape and is kept."""
        from issue_orchestrator.domain.standing_ruling import without_binding_sections

        title = f"Fix {BINDING_BEGIN} parsing {BINDING_END} for good"
        section = rulings_prompt(7, (self.RULING,), RulingsAudience.CODER)

        assert without_binding_sections(f"Work on: {title}\n\n{section}") == f"Work on: {title}\n\n"

    def test_a_prompt_persisted_before_the_markers_drops_its_unmarked_section(self) -> None:
        """codex r4 F3: a retry recovered from a #8141 run carries its issue's
        section unmarked; a ruling retired since must not bind it."""
        from issue_orchestrator.domain.launch_prompt import RULINGS_SEPARATOR
        from issue_orchestrator.domain.standing_ruling import without_binding_sections

        marked = rulings_prompt(7, (self.RULING,), RulingsAudience.CODER)
        legacy = marked.removeprefix(f"{BINDING_BEGIN}\n").removesuffix(f"\n{BINDING_END}")

        assert without_binding_sections(f"{legacy}{RULINGS_SEPARATOR}Work on issue #7.") == "Work on issue #7."
        embedded = f"# Validation Retry\n\n{legacy}{RULINGS_SEPARATOR}Work on issue #7."
        assert without_binding_sections(embedded) == "# Validation Retry\n\nWork on issue #7."
