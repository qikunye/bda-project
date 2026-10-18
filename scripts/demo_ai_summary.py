"""Generate one grounded case summary with Amazon Bedrock.

This makes a real, billed Bedrock call (usually one, at most two). It reads a
bounded LLM input JSON file, defaulting to the checked-in synthetic example.
"""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.ai.llm_client import BedrockConverseClient, create_bedrock_runtime_client
from src.ai.summary import generate_case_summary
from src.config import AWS_REGION, BEDROCK_MODEL_ID

DEFAULT_INPUT = (
    PROJECT_ROOT / "docs" / "handovers" / "examples" / "llm_input.synthetic.json"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--model-id", default=BEDROCK_MODEL_ID)
    args = parser.parse_args()

    llm_input = json.loads(args.input.read_text(encoding="utf-8"))
    client = BedrockConverseClient(
        create_bedrock_runtime_client(AWS_REGION),
        model_id=args.model_id,
    )
    result = generate_case_summary(llm_input, client)

    print(f"Status: {result.status}")
    print(f"Model: {result.model_id} | prompt {result.prompt_version}")
    for number, attempt in enumerate(result.attempts, start=1):
        completion = attempt.completion
        print(
            f"Attempt {number}: {completion.usage.input_tokens} in / "
            f"{completion.usage.output_tokens} out tokens, "
            f"{completion.latency_ms} ms, "
            f"{len(attempt.issues)} problem(s)"
        )
        for issue in attempt.issues:
            print(f"  - [{issue.code}] {issue.message}")
        if attempt.issues and completion.payload is not None:
            print(f"  rejected answer: {completion.payload.get('answer')}")

    if result.response is not None:
        print("\nSummary:\n" + result.response.answer)
        print("\nEvidence:")
        for reference in result.response.evidence:
            print(f"  {reference.type}: {reference.id}")
        print("Limitations:")
        for limitation in result.response.limitations:
            print(f"  {limitation.code}: {limitation.detail}")
    if result.pattern_summary:
        print("\nAccount patterns (from the data, not the model):\n" + result.pattern_summary)
    return 0 if result.status == "grounded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
