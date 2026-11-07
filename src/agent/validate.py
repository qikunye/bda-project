"""Checks the LLM's brief against what the tools returned in the same run.
It rejects unsupported findings, made-up IDs, disallowed next checks and numbers in the summary."""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from src import config
from src.agent import brief as brief_rules
from src.agent import findings as finding_rules
from src.agent.contracts import Brief, Finding
from src.agent.evidence import RunEvidence

FINDING_CODES = frozenset(finding_rules.FINDING_CODES)
NEXT_CHECK_CODES = frozenset(brief_rules.NEXT_CHECK_CODES)
BRIEF_FIELDS = frozenset({"findings", "next_checks", "summary"})
# "one" is left out because it is also an ordinary word, as in "one of the accounts".
_NUMBER_WORD = re.compile(
    r"\b(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
    r"sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    r"hundreds?|thousands?|millions?|billions?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Validated:
    brief: Brief | None
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.brief is not None


def _parse_shape(payload) -> tuple[Brief | None, list[str]]:
    """Checks the brief has the right fields and allowed values."""

    if not isinstance(payload, Mapping):
        return None, ["The brief must be a JSON object."]
    errors: list[str] = []

    unknown = sorted(set(payload) - BRIEF_FIELDS)
    if unknown:
        hint = (
            " case_id, gaps and money_flow are set by code, so leave them out."
            if {"case_id", "gaps", "money_flow"} & set(unknown)
            else ""
        )
        errors.append(f"Unknown field(s): {unknown}.{hint}")

    findings: list[Finding] = []
    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        errors.append("findings must be a list of {code, evidence_ids}. Use [] for none.")
        raw_findings = []
    seen: set[str] = set()
    for raw in raw_findings:
        if not isinstance(raw, Mapping) or set(raw) != {"code", "evidence_ids"}:
            errors.append(
                "Each finding must have exactly the fields code and evidence_ids."
            )
            continue
        code = raw["code"]
        if code not in FINDING_CODES:
            errors.append(
                f"Unknown finding code {code!r}. Valid: {sorted(FINDING_CODES)}."
            )
            continue
        if code in seen:
            errors.append(f"Finding {code} appears twice. List each code once.")
            continue
        seen.add(code)
        ids = raw["evidence_ids"]
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            errors.append(f"{code} evidence_ids must be a list of ID strings.")
        elif not ids:
            errors.append(f"Finding {code} needs at least one evidence ID.")
        elif len(ids) > config.AGENT_MAX_EVIDENCE_IDS:
            errors.append(
                f"Finding {code} cites {len(ids)} IDs. The limit is {config.AGENT_MAX_EVIDENCE_IDS}."
            )
        else:
            findings.append(Finding(code=code, evidence_ids=list(dict.fromkeys(ids))))

    next_checks: list[str] = []
    raw_checks = payload.get("next_checks")
    if not isinstance(raw_checks, list):
        errors.append("next_checks must be a list of codes. Use [] for none.")
        raw_checks = []
    for code in raw_checks:
        if code not in NEXT_CHECK_CODES:
            errors.append(
                f"Unknown next check {code!r}. Valid: {sorted(NEXT_CHECK_CODES)}."
            )
        elif code not in next_checks:
            next_checks.append(code)

    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary must be a short sentence.")
        summary = ""

    if errors:
        return None, errors
    return (
        Brief(findings=findings, next_checks=next_checks, summary=summary.strip()),
        [],
    )


def _summary_errors(summary: str) -> list[str]:
    errors: list[str] = []
    if len(summary) > config.AGENT_SUMMARY_MAX_CHARS:
        errors.append(
            f"summary is {len(summary)} characters. The limit is {config.AGENT_SUMMARY_MAX_CHARS}."
        )
    if any(character.isdigit() for character in summary):
        errors.append("summary must not contain digits. Remove them.")
    spelled = _NUMBER_WORD.search(summary)
    if spelled:
        errors.append(
            f"summary must not spell out numbers, but it says {spelled.group()!r}. Remove it."
        )
    return errors


def validate_brief(payload, evidence: RunEvidence) -> Validated:
    fields, errors = _parse_shape(payload)
    if fields is None:
        return Validated(None, tuple(errors))

    # 2. Every cited ID came from a tool result in this run.
    for finding in fields["findings"]:
        unseen = [i for i in finding["evidence_ids"] if i not in evidence.known_ids]
        if unseen:
            errors.append(
                f"Finding {finding['code']} cites IDs no tool returned in this run: {unseen[:5]}."
            )

    # 3. Each finding meets its rule, using this run's tool results.
    instances = finding_rules.detect(evidence)
    for finding in fields["findings"]:
        problem = finding_rules.check_finding(
            finding["code"], set(finding["evidence_ids"]), instances
        )
        if problem:
            errors.append(problem)

    # 4. Each next check is allowed by a finding it submitted or a gap code sets.
    gap_codes = {gap["code"] for gap in brief_rules.compute_gaps(evidence)}
    finding_codes = {f["code"] for f in fields["findings"]}
    allowed = set(brief_rules.allowed_next_checks(finding_codes, gap_codes))
    for check in fields["next_checks"]:
        if check not in allowed:
            needs = sorted(brief_rules.NEXT_CHECK_NEEDS[check])
            errors.append(
                f"{check} needs one of {needs} among the findings or gaps. Leave it out, "
                "or add the finding it follows from."
            )

    # 5. The summary is short and has no numbers, so every displayed number comes from code.
    errors.extend(_summary_errors(fields["summary"]))

    if errors:
        return Validated(None, tuple(errors))
    return Validated(fields)
