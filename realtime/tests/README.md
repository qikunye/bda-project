# Real-time local regression scripts

Run from the repository root using the root environment:

```bash
.venv/bin/python realtime/tests/test_local.py
RT_SKIP_MODEL=1 AWS_DEFAULT_REGION=us-east-1 AWS_ACCESS_KEY_ID=offline AWS_SECRET_ACCESS_KEY=offline AWS_EC2_METADATA_DISABLED=true .venv/bin/python realtime/tests/test_handler.py
AWS_DEFAULT_REGION=us-east-1 AWS_ACCESS_KEY_ID=offline AWS_SECRET_ACCESS_KEY=offline AWS_EC2_METADATA_DISABLED=true .venv/bin/python realtime/tests/test_explainer.py
.venv/bin/python realtime/tests/test_session_helper.py
```

The scripts resolve runtime imports from their own location and can also run
from `realtime/` as `python3 tests/<name>.py`. `release.py` uses these paths for
its three existing local gates. Do not run the release workflow for offline tests:
its later steps build/push images, deploy and evaluate AWS resources.

| Script | Coverage / prerequisites |
|---|---|
| `test_local.py` | Synthetic engine features versus a brute-force reference, including same-minute ties; standard library only |
| `test_handler.py` | In-memory DynamoDB, injected crash/retry, duplicate delivery and state/feature parity; model loading disabled; boto3 needed |
| `test_explainer.py` | Label allowlist, number grounding, schema, fallback, duplicate delivery and fake-model handler; boto3 needed; no Qwen weights |
| `test_session_helper.py` | Moto-backed session drain/brief waiting, teardown and reaper behavior; requires `moto` with the mocked AWS services and boto3 |

These standalone checks are excluded by root `pytest.ini` (`testpaths = tests`).
The scorer's model/data parity gate (`../offline_gate.py`) stays in the image
build context and needs the pinned model/schema plus a replay slice and Gold
reference. It cannot be established by the synthetic local tests.
