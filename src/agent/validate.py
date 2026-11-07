"""Checks the LLM's brief against what the tools returned in the same run.
It rejects unsupported findings, made-up IDs, disallowed next checks, and numbers or accusations
in the summary. Observations are optional extras, so a bad one is dropped instead of rejecting the brief."""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from src import config
from src.agent import brief as brief_rules
from src.agent import findings as finding_rules
from src.agent.contracts import Brief, Finding, Observation
from src.agent.evidence import RunEvidence

FINDING_CODES = frozenset(finding_rules.FINDING_CODES)
NEXT_CHECK_CODES = frozenset(brief_rules.NEXT_CHECK_CODES)
BRIEF_FIELDS = frozenset({"findings", "next_checks", "summary", "observations"})
# "one" is left out because it is also an ordinary word, as in "one of the accounts".
_NUMBER_WORD = re.compile(
    r"\b(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
    r"sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    r"hundreds?|thousands?|millions?|billions?)\b",
    re.IGNORECASE,
)


# The agent describes payments and never accuses anyone or gives a verdict, so these words, including
# AML terms of art for laundering stages and roles, are not allowed in its text.
_ACCUSATION = re.compile(
    r"\b(launder\w*|illicit|criminals?|crimes?|fraud\w*|guilty|suspicio\w*|suspect\w*|mules?|"
    r"layering|structuring|smurf\w*)\b",
    re.IGNORECASE,
)

# Wording that describes a finding's pattern. An observation may use it only when the brief has that
# finding, so a pattern the rules did not confirm cannot come back as an unchecked remark.
_FINDING_WORDING: dict[str, re.Pattern[str]] = {
    "PASS_THROUGH": re.compile(
        r"\b(pass(es|ed|ing)?|flow(s|ed|ing)?)[\s-]*through\b|\bpassthrough\b", re.IGNORECASE
    ),
    "FAN_OUT": re.compile(r"\bfan(s|ned|ning)?[\s-]*out\b", re.IGNORECASE),
    "FAN_IN": re.compile(r"\bfan(s|ned|ning)?[\s-]*in\b", re.IGNORECASE),
    "MONEY_RETURNS": re.compile(
        r"\b(went|goes|go|came|comes|come|flow(s|ed)?|sent|paid|cycl\w*|loop\w*) back\b|"
        r"\breturn(s|ed|ing)? to (the |its |their )?(sender|source|origin)\b|"
        r"\bround[\s-]?trip\w*|\bcircular\w*",
        re.IGNORECASE,
    ),
    "NEIGHBOURS_HIGH_RISK": re.compile(r"\bhigh(ly)?[\s-]*(risk|rank\w*|scor\w*)\b", re.IGNORECASE),
    "REPEAT_ACCOUNT_ALERTS": re.compile(
        r"\b(other|repeat\w*|related|further|multiple|several|more|earlier|later) alerts?\b|"
        r"\balerted (again|repeatedly)\b",
        re.IGNORECASE,
    ),
}
assert set(_FINDING_WORDING) == FINDING_CODES

# Claims no rule can back. A jump in activity was dropped as a finding because the account history
# (from 11 Sep) is too short and overlaps the window, so an observation may not claim one either.
_UNSUPPORTED_WORDING = re.compile(
    r"\bspik\w*|\bsurg(e|es|ed|ing)\b|\bbursts?\b|\bjump\w* in (activity|payments)\b|"
    r"\b(busier|more active) than (usual|before|normal)\b|\bunusual(ly)? (high|busy|active)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Validated:
    brief: Brief | None
    errors: tuple[str, ...] = ()
    # Why observations were left out of an accepted brief. They never cause a rejection.
    dropped_observations: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.brief is not None


def _parse_observations(payload: Mapping) -> tuple[list[Observation], list[str]]:
    """The well-formed observations, and why the others were dropped. Never rejects the brief."""

    raw_observations = payload.get("observations", [])
    if not isinstance(raw_observations, list):
        return [], ["observations was not a list, so none were kept."]
    dropped: list[str] = []
    if len(raw_observations) > config.AGENT_MAX_OBSERVATIONS:
        dropped.append(
            f"Only the first {config.AGENT_MAX_OBSERVATIONS} of {len(raw_observations)} "
            "observations were kept."
        )
    observations: list[Observation] = []
    seen_text: set[str] = set()
    for number, raw in enumerate(raw_observations[: config.AGENT_MAX_OBSERVATIONS], start=1):
        label = f"observation {number}"
        if not isinstance(raw, Mapping) or set(raw) != {"text", "evidence_ids"}:
            dropped.append(f"{label} did not have exactly the fields text and evidence_ids.")
            continue
        text, ids = raw["text"], raw["evidence_ids"]
        if not isinstance(text, str) or not text.strip():
            dropped.append(f"{label} had no text.")
        elif not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            dropped.append(f"{label} evidence_ids was not a list of ID strings.")
        elif not ids:
            dropped.append(f"{label} cited no evidence IDs.")
        elif len(ids) > config.AGENT_MAX_EVIDENCE_IDS:
            dropped.append(
                f"{label} cited {len(ids)} IDs. The limit is {config.AGENT_MAX_EVIDENCE_IDS}."
            )
        elif text.strip() not in seen_text:
            seen_text.add(text.strip())
            observations.append(
                Observation(text=text.strip(), evidence_ids=list(dict.fromkeys(ids)))
            )
    return observations, dropped


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
    # Observations are filled in by validate_brief, which drops bad ones instead of rejecting.
    return (
        Brief(
            findings=findings,
            next_checks=next_checks,
            summary=summary.strip(),
            observations=[],
        ),
        [],
    )


def _text_errors(text: str, label: str, limit: int) -> list[str]:
    """Rules for the LLM's own words: short, no numbers (code supplies them), no accusations."""

    errors: list[str] = []
    if len(text) > limit:
        errors.append(f"{label} is {len(text)} characters. The limit is {limit}.")
    if any(character.isdigit() for character in text):
        errors.append(f"{label} must not contain digits. Remove them.")
    spelled = _NUMBER_WORD.search(text)
    if spelled:
        errors.append(
            f"{label} must not spell out numbers, but it says {spelled.group()!r}. Remove it."
        )
    accusation = _ACCUSATION.search(text)
    if accusation:
        errors.append(
            f"{label} must describe payments, not accuse anyone, but it says "
            f"{accusation.group()!r}. Reword it."
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

    # 5. The summary is short, carries no numbers and makes no accusation.
    errors.extend(_text_errors(fields["summary"], "summary", config.AGENT_SUMMARY_MAX_CHARS))

    if errors:
        return Validated(None, tuple(errors))

    # 6. Observations cite IDs a tool returned, follow the same wording rules, and describe no pattern
    # the brief's findings do not have. Whether one is right is not checked. A bad one is dropped.
    observations, dropped = _parse_observations(payload)
    kept: list[Observation] = []
    for number, observation in enumerate(observations, start=1):
        label = f"observation {number}"
        problems = _text_errors(observation["text"], label, config.AGENT_OBSERVATION_MAX_CHARS)
        unseen = [i for i in observation["evidence_ids"] if i not in evidence.known_ids]
        if unseen:
            problems.append(f"{label} cites IDs no tool returned in this run: {unseen[:5]}.")
        claimed = [
            code
            for code, wording in _FINDING_WORDING.items()
            if code not in finding_codes and wording.search(observation["text"])
        ]
        if claimed:
            problems.append(
                f"{label} describes {', '.join(claimed)}, which is not among the findings."
            )
        unsupported = _UNSUPPORTED_WORDING.search(observation["text"])
        if unsupported:
            problems.append(
                f"{label} claims a change in activity ({unsupported.group()!r}), which the account "
                "history is too short to show."
            )
        if problems:
            dropped.extend(problems)
        else:
            kept.append(observation)

    fields["observations"] = kept
    return Validated(fields, dropped_observations=tuple(dropped))
