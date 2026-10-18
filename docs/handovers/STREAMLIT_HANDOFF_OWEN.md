IS459 AML Project: Stage 5 Dashboard (Athena and Streamlit)
As of 11 October 2026
Status at a glance
Stage 5 (Athena and Streamlit dashboard) is built. It answers BQ1 and BQ2 in one app and reads the Gold tables the BQ1 and BQ2 owners produced. It has been run on the real tables; the items below say exactly what was checked.
Item	Status
Alert queue screen (BQ1): ranked alerts, review budget, detection curve	Done, run on the real tables (63.4% found at the top 1%, 6,250,304 test rows)
“Where alerts concentrate” view (BQ1)	Done, run on the real tables
Reasons file (“Why it was flagged”)	Done: 62,504 rows, the riskiest 1% of the test days
Money trail screen (BQ2): graph, case summary, timeline, window sensitivity	Done, BQ2 logic unchanged from the BQ2 app
“Which connected accounts to check first” (BQ2 + BQ1 scores)	Built and tested on demo data; the first real-data run is still to be checked
Automated tests	30 passing, all on demo data (no AWS needed)
Demo mode (runs with no AWS)	Done, for rehearsal and recording
Where the app runs for the final demo, CSV vs Parquet benchmark, AWS cleanup	Not decided or not started (see Open issues and cleanup)
All data is synthetic (IBM AML dataset). Nothing in the app is a real-world laundering rate, and a connected account is a lead to investigate, not proof of anything.
Where everything lives
The app is in the zip AML_Streamlit_App.zip (also in the project folder under Streamlit app). It reads from AWS account g1team5, region us-east-1, and writes nothing except Athena’s own result files.
What	Name / path	Notes
Entry point	app.py	Two screens. Run with streamlit run app.py
Settings	aml_ui/config.py	Table names, test dates (14 to 16 Sep), budget options, window settings. Overrides: AML_DEMO, AML_ATHENA_OUTPUT, AML_AWS_REGION, AML_ATHENA_WORKGROUP, AML_REASONS_FILE
SQL	aml_ui/sql.py	Every statement the app sends. The BQ1 queue, budget and curve queries are the BQ1 owner’s, parameterised. The BQ2 traversal query is the BQ2 owner’s
Input checks	aml_ui/validate.py	Every value placed in SQL is checked first (dates, account keys, transaction IDs)
Athena runner	aml_ui/athena.py	Per-session query log with bytes scanned, shown in the sidebar
Live and demo data	aml_ui/backend.py, aml_ui/demo_data.py	Same interface; demo is made-up data
Charts and numbers	aml_ui/insights.py	“Where alerts concentrate” and the trail timeline
BQ2 logic	aml_ui/trail.py	Window rule, aggregation, window sensitivity, node building, graph drawing (the BQ2 owner’s code), plus the connected-account ranking
Reasons wording	aml_ui/describe.py	Turns a model feature into a plain sentence
Reasons file	data/bq1_explanations_top1pct.csv	One row per alert: three reasons with direction and size. Keyed by transaction_id
Reasons script	explanations/make_explanations.py	Rebuilds the reasons file from the BQ1 model
BQ1 model files	s3://is459-aml-g1t5/models/bq1/xgboost/v1/	model.ubj, feature_schema.json, model_metadata.json, metrics.json. Not in the zip; only needed to rebuild the reasons
Tables read (read-only)	is459_aml_gold.bq1_risk_scores_medium_v1, bq1_features_medium_v1, bq2_graph_edges_medium_v1	31,251,483 scored rows; 28,752,132 graph edges
Athena results	s3://is459-aml-g1t5/athena-results/bq2/	The folder the BQ2 app already used. Change with AML_ATHENA_OUTPUT
Tests	tests/	30 tests, run with pytest tests -q
What the app shows
Alert queue (BQ1: where should the analyst start?). A review-budget slider (top 0.1% to 10% of the test queue) drives the whole screen, then a switch:
View	What it shows
Ranked alerts	The riskiest transactions in review order, a “Main reason” column, and a selected-alert panel with all three reasons. A button opens the money trail for the selected alert. The detection curve (laundering found against review effort) sits in a collapsed box
Where alerts concentrate	Where the flagged transactions cluster, in alerts per 1,000 transactions against the overall average: payment formats, time of day, banks with more alerts than their size suggests, accounts that keep appearing in alerts, and what a flagged transaction looks like (only the traits that set alerts apart; the rest are listed as “no real difference”)
Money trail (BQ2: where should the analyst investigate next?). An investigation window slider (6 to 72 hours, default 48), then:
Part	What it shows
Network graph	The flagged transfer, accounts that paid into the sender before it (upstream) and accounts the receiver paid after it (downstream)
Case summary	Connected, upstream and downstream account counts
Which connected accounts to check first	Connected accounts ranked by the BQ1 score of their transfer with the flagged accounts. A tick means the transfer fell on a held-out test day, so the score is fair; no tick means indicative only
Timeline	Each connected transaction on a time axis, before or after the alert
Window sensitivity	How many connected accounts each window finds (6, 12, 24, 30, 48, 72 hours)
Underlying transactions	The raw transfers behind the graph
Validation evidence
Check	Result
Headline numbers on the real tables	63.4% of laundering-labelled transactions found at the top 1%, 6,250,304 test rows, matching the BQ1 handover
Automated tests (demo data)	30 passing: both screens and the concentration view load, the click-through works, the concentration counts add up, the budget carries between views, the connected-account ranking is ordered, bad input is rejected
SQL	Every statement parses as Athena (Trino) SQL in the tests; values placed in SQL are checked first
Reasons script	Tested end to end on a small stand-in model, and against the real model file (loads, 27 inputs, scores a test row). On the real rows it compares with the stored risk_score before writing
Reasons file	62,504 rows, equal to the top 1% of the test queue
To reproduce the tests (about 30 seconds, no AWS):
pip install -r requirements.txt
pytest tests -q
Running the app
Demo mode (no AWS, made-up data). Windows PowerShell, from the app folder:
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:AML_DEMO = "1"
streamlit run app.py
(Command Prompt: .venv\Scripts\activate.bat, then set AML_DEMO=1. Mac or Linux: source .venv/bin/activate, then AML_DEMO=1 streamlit run app.py.) The page opens at http://localhost:8501 and shows a yellow “Demo mode” box in the sidebar.
Real data:
1.	Sign in to AWS on your own computer (aws configure or your SSO login) for account g1team5, region us-east-1. Never put AWS keys in the code, in the folder or in chat.
2.	Make sure AML_DEMO is not set (new terminal, or Remove-Item Env:AML_DEMO).
3.	Run streamlit run app.py.
If you change code while the app is running, stop it with Ctrl+C and start it again. A browser refresh keeps the old code in memory (the symptom is an error such as “‘AthenaBackend’ object has no attribute …”).
Rebuilding the reasons file
Only needed if the model or the score table changes. You need the model folder and an AWS login:
1.	Download model.ubj and feature_schema.json (and the two small JSON files) from models/bq1/xgboost/v1/ into a local folder, for example BQ1/model_v1. The console cannot download a folder, so use aws s3 cp --recursive or download the files one at a time.
2.	From the app folder run: python explanations/make_explanations.py --model-dir BQ1/model_v1
3.	Refresh the app.
The script reads the model inputs for the riskiest 1% of the test days from Athena (62,504 rows), checks its scores against the stored ones, and writes data/bq1_explanations_top1pct.csv. A reason such as “↑ First payment between these two accounts” means that fact raised the score; “↓” means it lowered it. These are the model’s own explanation of its score, not proof of laundering.
Answers to the BQ1 owner’s five questions: (1) explanations are provided, the top three features per alert with direction and size; (2) in a separate file keyed by transaction_id; (3) for the top 1% of the test queue; (4) as feature name, signed number and a plain sentence; (5) nothing else is needed in the score table.
Cost and performance
•	Athena is billed by data scanned. The sidebar shows each query’s bytes scanned for the session.
•	Looking up one alert’s money trail scans about 0.9 GB, because transaction_id is not a partition key. The queue and budget queries filter on txn_date.
•	The first load of “Where alerts concentrate” joins the scores to the features table for the three test days and takes about a minute. It then caches for an hour.
Next steps for the rest of the project
•	Rehearse and record the walkthrough in demo mode first, then once on the real data. Suggested order: set the review budget, read the concentration view, pick an alert, read its reasons, open its money trail, move the window slider.
•	Pick a demo alert that exists in both the BQ1 and BQ2 tables so the click-through works live.
•	CSV vs Parquet benchmark for the X-factor, if the team uses it. The app’s query log already records bytes scanned and run time for every query.
•	Report sentences. The queue answers BQ1 (“where should the analyst start?”) and the money trail answers BQ2 (“where should the analyst investigate next?”). Say that connected accounts are leads, not proof, and that scores are measured on the held-out test days.
Open issues and cleanup
•	Connected-account scores on real data. The lookup behind “Which connected accounts to check first” has been tested on demo data only. Check the sidebar query log on first use.
•	Where the app runs for the final demo. A laptop with the AWS login works; hosting it elsewhere needs a team decision about credentials.
•	LI-HI-Medium. Whether it is in or out of scope is still open.
•	Network explorer. The task breakdown mentions an account-seeded network explorer; confirm with the rubric whether the money trail covers it.
•	Known limits. Self-transfers have no trail. Groups with fewer than 500 transactions are left out of the concentration charts. Training-day scores in the connected-account list are indicative only.
•	Cleanup after the Week 14 presentation: delete the AWS resources (see the cleanup steps in the other handovers). This app creates nothing in AWS except Athena result files under athena-results/bq2/.
