"""The one place a tech-lead decision is recorded as having reached GitHub (#7080).

"Has any tech-lead decision landed lately?" had no answer for ten days, and the
reason was that nothing owned the question. ``TECH_LEAD_ACTION_EXECUTED`` was
emitted only by the act-level executors -- ``reset_retry``,
``kill_hung_session``, scoped rework, validated-work recovery -- every one of
which defaults to ``propose`` authority and waits for an approval that may never
arrive. So the store held ZERO such rows while the tech lead filed follow-ups and
posted diagnoses daily, and the aggregate signal could not distinguish "nothing
is landing" from "landing is not reported".

Two rules make a receipt trustworthy, and both are here rather than at each
call site, because a rule enforced at four call sites is four rules:

* **Applied, not merely filed.** A gated proposal is created on GitHub and is
  deliberately INERT until an operator removes its ``proposed-tech-lead`` label.
  Reporting it as an executed decision would say "the tech lead is writing" for
  precisely the state #7080 is about -- decisions piling up unapproved (#7262
  review F2).
* **Every applied effect, not one favoured kind.** An execute-authority
  ``post_comment`` and a human disposition both reach GitHub and both are
  decisions; counting only ``create_issue`` reported a healthy comment-only
  health review as ``silent`` (#7262 review F3).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..events import EventName
from ..ports import make_trace_event

if TYPE_CHECKING:
    from ..ports import EventSink


def record_decision_applied(
    events: "EventSink",
    *,
    anchor_issue_number: int,
    action: str,
    **detail: Any,
) -> None:
    """Record that one tech-lead decision effect reached GitHub.

    ``anchor_issue_number`` is the run the decision belongs to, so a reader can
    correlate the effect with the run that decided it. ``action`` is the decision
    class in the vocabulary an operator already knows from
    ``tech_lead.authority.*`` (``create_issue``, ``post_comment``,
    ``escalate_to_human``, ...).

    Call this ONLY after the effect is known to have succeeded. A receipt for an
    attempt is worse than no receipt: it reports writing where there is none,
    which is the failure this exists to detect.
    """
    events.publish(
        make_trace_event(
            EventName.TECH_LEAD_ACTION_EXECUTED,
            {"issue_number": anchor_issue_number, "action": action, **detail},
        )
    )
