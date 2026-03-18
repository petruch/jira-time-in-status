# Jira Status Calculation Tool

A lightweight Python CLI for Jira Cloud that pulls issue changelogs and calculates time spent in each workflow status, helping identify bottlenecks and delivery delays.

## Features

- Connects to Jira Cloud using email and API token
- Searches issues using JQL
- Pulls full changelog history
- Calculates total time spent in each status
- Exports results to CSV
- Supports output in seconds, minutes, hours, or days
- Supports credentials from environment variables or macOS Keychain

## Requirements

- Python 3.9+
- Jira Cloud account
- Jira API token

## Installation


git clone <your-repo-url>
cd PythonJiraTool
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt


## Authentication

### Environment variables


export JIRA_BASE_URL="https://yourcompany.atlassian.net"
export JIRA_EMAIL="your.email@company.com"
export JIRA_API_TOKEN="your_api_token"


### macOS Keychain


security add-generic-password -a "$USER" -s jira_base_url -w "https://yourcompany.atlassian.net" -U
security add-generic-password -a "$USER" -s jira_email -w "your.email@company.com" -U
security add-generic-password -a "$USER" -s jira_api_token -w "your_api_token" -U


## Usage

Show help:


python3 src/jirapythontool/cli.py -h


Basic example:

python3 src/jirapythontool/cli.py \
  --project XXX \
  --issue-type Story \
  --since-days 100 \
  --time-unit hours \
  --out XXX.csv


Custom JQL example:


python3 src/jirapythontool/cli.py \
  --jql 'project = "XXX" AND issuetype in (Story, Bug) AND status = "Done"' \
  --time-unit days \
  --out XXX.csv

Optional status selection and ordering
You can control which status columns appear in the CSV and the order they appear in.
If --statuses is not provided, the script will automatically include all discovered statuses.


Examples:
python3 cli.py \
  --jql 'project = "Product Solution Design" AND issuetype in (Story, Bug)' \
  --statuses "To Do" "In Progress" "Blocked" "Review" "UAT" "Ready To Deploy" \
  --out XXXX.csv

  
Or comma-separated:
python3 cli.py \
  --jql 'project = "Product Solution Design" AND issuetype in (Story, Bug)' \
  --statuses "To Do,In Progress,Blocked,Review,UAT,Ready To Deploy" \
  --out XXXX.csv

  
Optional extra output fields
You can include additional Jira fields in the CSV before the status columns by using --extra-fields.

Use the format:
--extra-fields "Column Label=field_name"


Examples:

python3 cli.py \
  --jql 'project = "Product Solution Design" AND issuetype in (Story, Bug)' \
  --extra-fields "Priority=priority" \
  --out XXXX.csv
  
Custom field example:

python3 cli.py \
  --jql 'project = "Product Solution Design" AND issuetype in (Story, Bug)' \
  --extra-fields "Story Points=customfield_10026" "Priority=priority" \
  --out XXXX.csv
  
Combined example

python3 cli.py \
  --jql 'project = "Product Solution Design" AND issuetype in (Story, Bug) AND status = "Done"' \
  --time-unit days \
  --statuses "To Do,In Progress,Blocked,Review,UAT,Ready To Deploy" \
  --extra-fields "Story Points=customfield_10026" "Priority=priority" \
  --out XXXX.csv
  
Notes:

--statuses only controls which status columns are shown in

## Output

The tool generates a CSV where each row is an issue and each status column contains the total time spent in that status.

Example:


issue_key,summary,assignee,To Do,In Progress,Blocked,Done
XXX-101,Create validation rule,Jane Doe,1.20,3.50,0.75,2.10


## How It Works

1. Search Jira issues using JQL
2. Pull changelog history for each issue
3. Extract status transitions
4. Calculate time between transitions
5. Sum total time per status
6. Export results to CSV

## Notes

* Time is calculated using calendar time, not business hours
* The final status interval is calculated up to the current time
* Large queries may take longer because changelog data is fetched per issue

## Troubleshooting

* Use straight quotes, not curly quotes
* Do not leave spaces after `\` in multi-line commands
* If the shell shows `quote>`, one of your quotes is not closed
