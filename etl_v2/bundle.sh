#!/bin/bash
# Read-only: bundle every etl-v2 Glue job script, its arguments, the state machine and the Lambda code
# into one text file, upload it to S3 and print a short-lived presigned URL that renders as text.
set -u
cd ~/etlv2 || exit 1
OUT=bundle.txt
: > $OUT
aws glue get-jobs --query 'Jobs[?starts_with(Name,`is459_etl_v2`)].[Name,Command.ScriptLocation]' --output text > jobs.txt
declare -A seen
while read -r n s; do
  {
    echo "########## JOB $n | $s"
    aws glue get-job --job-name "$n" --query 'Job.{Role:Role,Glue:GlueVersion,Worker:WorkerType,N:NumberOfWorkers,Cmd:Command.Name,Py:Command.PythonVersion,Args:DefaultArguments,Timeout:Timeout,Tags:Tags}' --output json
  } >> $OUT
  if [ -z "${seen[$s]:-}" ]; then
    seen[$s]=1
    aws s3 cp "$s" - >> $OUT 2>&1
  else
    echo "(script already printed above)" >> $OUT
  fi
done < jobs.txt
{
  echo "########## SFN"
  aws stepfunctions describe-state-machine --state-machine-arn arn:aws:states:us-east-1:941377154820:stateMachine:is459-etl-v2-silver-wap --query '[roleArn,definition]' --output text
  echo "########## SCHEDULE"
  aws scheduler get-schedule --name is459-etl-v2-silver-schedule --output json
  echo "########## LAMBDA"
  aws lambda get-function-configuration --function-name is459-etl-v2-promote-gold --output json
  curl -s "$(aws lambda get-function --function-name is459-etl-v2-promote-gold --query Code.Location --output text)" -o l.zip && unzip -o -q l.zip -d lambda && for f in $(find lambda -type f); do echo "---- $f"; cat "$f"; done
  echo "########## IAM ROLES"
  for r in is459-etl-v2-glue-role; do
    aws iam list-attached-role-policies --role-name $r --output json
    for p in $(aws iam list-role-policies --role-name $r --query 'PolicyNames' --output text); do echo "-- inline $p"; aws iam get-role-policy --role-name $r --policy-name $p --query PolicyDocument --output json; done
  done
} >> $OUT 2>&1
aws s3 cp $OUT s3://is459-aml-g1t5/results/etl-v2/audit-20261017/bundle.txt --content-type text/plain >/dev/null
python3 - <<'EOF'
import boto3
s=boto3.client("s3",region_name="us-east-1")
print(s.generate_presigned_url("get_object",Params={"Bucket":"is459-aml-g1t5","Key":"results/etl-v2/audit-20261017/bundle.txt","ResponseContentType":"text/plain; charset=utf-8"},ExpiresIn=3600))
EOF
wc -c $OUT
