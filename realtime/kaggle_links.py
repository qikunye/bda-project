#!/usr/bin/env python3
"""Short-lived links for the Kaggle explainer notebook (no AWS keys ever go to Kaggle).
CloudShell credentials expire within minutes, so links are signed with a dedicated role assumed for 1 hour.
  python3 kaggle_links.py get                 presigned GET for rt/explainer-data/candidates.csv.gz (1 h)
  python3 kaggle_links.py put <model_version>  JSON {file: presigned PUT url} for the notebook's upload step (1 h)
The role can only read the candidates file and write the model folder and the two eval files."""
import json, sys, time
import boto3

R, B, ACCT = 'us-east-1', 'is459-aml-g1t5', '941377154820'
ROLE = 'is459-rt-kaggle-transfer'
MODEL_FILES = ['model.gguf', 'model.sha256', 'base.gguf', 'base.sha256', 'typology_clf.json', 'typology_clf_meta.json',
               'training_card.json', 'teacher_stats.json', 'teacher_eval_ref.jsonl', 'sft_train.jsonl']
DATA_FILES = ['eval_sample.jsonl', 'prep_metrics.json', 'heldout.jsonl']
iam = boto3.client('iam')


def role_arn():
    trust = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Principal': {'AWS': 'arn:aws:iam::%s:root' % ACCT}, 'Action': 'sts:AssumeRole'}]}
    try:
        arn = iam.get_role(RoleName=ROLE)['Role']['Arn']
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust), MaxSessionDuration=3600,
                              Tags=[{'Key': 'project', 'Value': 'is459-g1t5-rt'}],
                              Description='Signs 1-hour S3 links for the Kaggle explainer notebook')['Role']['Arn']
        time.sleep(10)
    iam.put_role_policy(RoleName=ROLE, PolicyName='KaggleTransfer', PolicyDocument=json.dumps({'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Action': 's3:GetObject', 'Resource': 'arn:aws:s3:::%s/rt/explainer-data/candidates.csv.gz' % B},
        {'Effect': 'Allow', 'Action': 's3:PutObject', 'Resource': ['arn:aws:s3:::%s/rt/models/explainer/*' % B] +
         ['arn:aws:s3:::%s/rt/explainer-data/%s' % (B, f) for f in DATA_FILES]}]}))
    return arn


def s3_as_role():
    arn = role_arn()
    for i in range(6):
        try:
            c = boto3.client('sts').assume_role(RoleArn=arn, RoleSessionName='kaggle-links', DurationSeconds=3600)['Credentials']
            break
        except Exception as e:
            print('waiting for role', e, file=sys.stderr); time.sleep(10)
    return boto3.client('s3', region_name=R, aws_access_key_id=c['AccessKeyId'], aws_secret_access_key=c['SecretAccessKey'],
                        aws_session_token=c['SessionToken'])


if __name__ == '__main__':
    s3 = s3_as_role()
    if sys.argv[1] == 'get':
        print(s3.generate_presigned_url('get_object', Params={'Bucket': B, 'Key': 'rt/explainer-data/candidates.csv.gz'}, ExpiresIn=3600))
    else:
        ver = sys.argv[2]
        urls = {f: s3.generate_presigned_url('put_object', Params={'Bucket': B, 'Key': 'rt/models/explainer/%s/%s' % (ver, f)}, ExpiresIn=3600)
                for f in MODEL_FILES}
        urls.update({f: s3.generate_presigned_url('put_object', Params={'Bucket': B, 'Key': 'rt/explainer-data/' + f}, ExpiresIn=3600)
                     for f in DATA_FILES})
        print(json.dumps(urls))
