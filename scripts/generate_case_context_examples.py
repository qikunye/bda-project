"""Regenerate the documented CaseContext examples from synthetic fixtures."""

from pathlib import Path

from src.context.projection import build_llm_input
from src.context.validation import (
    serialize_case_context,
    validate_case_context,
    validate_llm_input,
)
from tests.unit.context_fixtures import make_context

EXAMPLE_DIRECTORY = Path("docs/handovers/examples")


def main() -> None:
    context = make_context()
    llm_input = build_llm_input(
        context,
        max_candidates=3,
        max_transactions=5,
    )
    validate_case_context(context)
    validate_llm_input(llm_input)

    EXAMPLE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    (EXAMPLE_DIRECTORY / "case_context.synthetic.json").write_text(
        serialize_case_context(context) + "\n",
        encoding="utf-8",
    )
    (EXAMPLE_DIRECTORY / "llm_input.synthetic.json").write_text(
        serialize_case_context(llm_input) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
