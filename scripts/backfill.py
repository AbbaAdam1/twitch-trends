"""Re-run the deployed compactor for a range of days, oldest first.

Use it to load the days from before the compactor existed, or to rebuild days after a fix.
Each day replaces its own files, so running a range twice is safe.

    python scripts/backfill.py --start 2026-11-02 --end 2026-11-05
"""

# It calls the real Lambda function instead of compacting on this computer, so a backfill
# runs exactly the same code as the daily schedule and the data never leaves AWS.

import argparse
import json
import sys
from datetime import date, timedelta

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

STACK_NAME = "TwitchTrendsPipeline"


def find_compactor(cloudformation) -> str:
    """Look up the compactor's real name. CDK adds random letters to it, so it can't be typed."""
    pages = cloudformation.get_paginator("list_stack_resources").paginate(StackName=STACK_NAME)
    for resource in pages.search("StackResourceSummaries[]"):
        if resource["ResourceType"] == "AWS::Lambda::Function" and resource[
            "LogicalResourceId"
        ].startswith("Compactor"):
            return resource["PhysicalResourceId"]
    raise LookupError(f"No compactor function in stack {STACK_NAME}")


def compact_day(lambda_client, function_name: str, dt: date) -> dict:
    """Run the compactor for one day and wait for its summary."""
    response = lambda_client.invoke(
        FunctionName=function_name, Payload=json.dumps({"dt": dt.isoformat()}).encode()
    )
    result = json.loads(response["Payload"].read())
    # A crash inside the function still returns HTTP 200; this field is how Lambda reports it.
    if "FunctionError" in response:
        raise RuntimeError(f"{dt}: {result.get('errorType')}: {result.get('errorMessage')}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", type=date.fromisoformat, help="YYYY-MM-DD (default: --start)")
    parser.add_argument("--profile", default="twitch-trends")
    args = parser.parse_args()
    end = args.end or args.start
    if end < args.start:
        parser.error("--end is before --start")

    session = boto3.Session(profile_name=args.profile, region_name="us-east-1")
    # A day can take a few minutes, longer than boto3's default wait. No automatic retries:
    # a retry would be safe, but a failure is clearer to read than a silent second attempt.
    lambda_client = session.client(
        "lambda", config=Config(read_timeout=900, retries={"total_max_attempts": 1})
    )

    try:
        function_name = find_compactor(session.client("cloudformation"))
        dt = args.start
        while dt <= end:
            summary = compact_day(lambda_client, function_name, dt)
            print(
                f"{dt}: {summary['snapshots_compacted']}/{summary['snapshot_files']} snapshots, "
                f"{summary['rows_written']:,} rows, {summary['rows_rejected']:,} rejected "
                f"{summary['rejected_by_reason']}"
            )
            dt += timedelta(days=1)
    except (ClientError, LookupError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
