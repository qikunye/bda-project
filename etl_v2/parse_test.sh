#!/bin/bash
# Section 3a parse parity for a day that is already in Silver main (14, 15, 16 Sep).
# Runs the audit job in --mode parse-test: parse the day from the raw CSV into
# results/etl-v2/parse-test/<day>/parse-test-<daycompact>-<tag>/, run the 11 DQ rules there, compare all 33 columns with Silver main
# both ways. It never creates a branch and never publishes. Then checks main and the branch list did not move.
# Usage: ./parse_test.sh 2022-09-15 [tag] [source_csv_key]
set -euo pipefail
DAY=$1; TAG=${2:-$(date -u +%Y%m%d%H%M)}; SRC=${3:-bronze/ibm-aml/li-medium/transactions/}
DC=${DAY//-/}; B=is459-aml-g1t5; JOB=is459_etl_v2_job-silver-audit-dq
RUN=parse-test-$DC-$TAG
refs() { aws lambda invoke --function-name is459-etl-v2-main-snapshot --cli-binary-format raw-in-base64-out --payload '{"refs":true}' /tmp/ms.json >/dev/null && cat /tmp/ms.json; }
BEFORE=$(refs); echo "before: $BEFORE"
for i in 1 2 3 4 5 6 7 8 9 10; do   # the previous run can take a minute to release the single concurrency slot
  JR=$(aws glue start-job-run --job-name $JOB --arguments "{\"--mode\":\"parse-test\",\"--run-id\":\"$RUN\",\"--day\":\"$DAY\",\"--source-csv-key\":\"$SRC\",\"--audit-branch\":\"none\",\"--expected-main-snapshot\":\"none\"}" --query JobRunId --output text 2>/tmp/sjr.err) && break
  grep -q ConcurrentRunsExceeded /tmp/sjr.err || { cat /tmp/sjr.err; exit 1; }; sleep 30
done
echo "job run $JR"
while true; do
  ST=$(aws glue get-job-run --job-name $JOB --run-id $JR --query JobRun.JobRunState --output text)
  case $ST in SUCCEEDED|FAILED|ERROR|TIMEOUT|STOPPED) break;; esac; sleep 30
done
echo "state $ST"
aws s3 cp s3://$B/results/etl-v2/parse-test/$DAY/$RUN/result.json - | python3 -c "import json,sys;r=json.load(sys.stdin);print(json.dumps({k:r.get(k) for k in ('status','rows','source_proof_match','parity','dq_rules_passed','main_mutated','error')},indent=1))" || true
AFTER=$(refs); echo "after:  $AFTER"
python3 - "$BEFORE" "$AFTER" <<'PY'
import json, sys
b, a = json.loads(sys.argv[1]), json.loads(sys.argv[2])
ok = b['main_snapshot'] == a['main_snapshot'] and b.get('refs') == a.get('refs')
print('MAIN_AND_BRANCHES_UNCHANGED' if ok else 'CHANGED: %s -> %s' % (b, a)); sys.exit(0 if ok else 1)
PY
[ "$ST" = SUCCEEDED ]
