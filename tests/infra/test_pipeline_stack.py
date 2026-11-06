"""Checks the generated AWS template, so cost and security rules can't quietly regress."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infra.pipeline_stack import PipelineStack
from twitch_trends.compactor.handler import STREAM_SNAPSHOTS_SCHEMA


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    stack = PipelineStack(app, "TestStack", alert_email="alerts@example.com")
    return Template.from_stack(stack)


def test_bucket_is_private_encrypted_and_survives_destroy(template):
    template.has_resource(
        "AWS::S3::Bucket",
        {
            "DeletionPolicy": "Retain",
            "Properties": {
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "BlockPublicPolicy": True,
                    "IgnorePublicAcls": True,
                    "RestrictPublicBuckets": True,
                },
                "BucketEncryption": Match.any_value(),
            },
        },
    )


def test_logs_expire_so_they_cannot_grow_forever(template):
    template.has_resource_properties("AWS::Logs::LogGroup", {"RetentionInDays": 14})


def test_collector_runs_every_10_minutes(template):
    template.has_resource_properties(
        "AWS::Events::Rule", {"ScheduleExpression": "rate(10 minutes)", "State": "ENABLED"}
    )


def test_release_sync_runs_daily(template):
    template.has_resource_properties(
        "AWS::Events::Rule", {"ScheduleExpression": "cron(0 6 * * ? *)", "State": "ENABLED"}
    )


def test_functions_are_small_and_outside_any_vpc(template):
    functions = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Runtime": "python3.12"}}
    )
    memory = {f["Properties"]["Handler"]: f["Properties"]["MemorySize"] for f in functions.values()}
    assert memory == {
        "twitch_trends.collector.handler.handler": 256,
        "twitch_trends.release_sync.handler.handler": 256,
        "twitch_trends.compactor.handler.handler": 1024,  # once a day; more memory, more CPU
    }
    for function in functions.values():
        properties = function["Properties"]
        assert properties["Architectures"] == ["arm64"]
        assert "VpcConfig" not in properties  # a VPC would need a costly NAT gateway


def test_no_expensive_resources(template):
    for resource_type in ("AWS::EC2::NatGateway", "AWS::Glue::Job", "AWS::Glue::Crawler"):
        template.resource_count_is(resource_type, 0)


def role_policy(template, function_id: str) -> str:
    """The permissions of one function's role, as JSON text."""
    policies = template.find_resources("AWS::IAM::Policy")
    matches = [
        policy
        for policy in policies.values()
        if policy["Properties"]["PolicyName"].startswith(f"{function_id}ServiceRoleDefaultPolicy")
    ]
    assert len(matches) == 1
    return json.dumps(matches[0])


def test_collector_permissions_are_narrow(template):
    policy = role_policy(template, "Collector")
    # It may add snapshots and read the watchlist, but can't read collected data or delete.
    assert "/raw/streams/*" in policy
    assert '"/ref/watchlist.json"' in policy
    assert "s3:GetObject*" not in policy  # the broad form that grant_read would add
    assert "s3:DeleteObject" not in policy
    assert "s3:*" not in policy
    assert '"s3:prefix": "ref/watchlist.json"' in policy  # listing limited to one file name
    # The only wildcard resource is the metric action, limited to one namespace.
    assert "cloudwatch:namespace" in policy
    # Exactly the two Twitch parameters, not every parameter in the account.
    assert ":parameter/twitch-trends/client-id" in policy
    assert ":parameter/twitch-trends/client-secret" in policy


def test_release_sync_can_only_write_reference_files(template):
    policy = role_policy(template, "ReleaseSync")
    assert "/ref/*" in policy
    assert "raw/streams" not in policy
    assert "s3:GetObject" not in policy
    assert "s3:DeleteObject" not in policy
    assert ":parameter/twitch-trends/client-secret" in policy


def test_compactor_reads_raw_data_and_writes_only_curated_files(template):
    policy = role_policy(template, "Compactor")
    assert '"s3:prefix": "raw/streams/*"' in policy
    assert "/raw/streams/*" in policy
    assert "/curated/*" in policy
    assert "s3:DeleteObject" not in policy
    assert "s3:*" not in policy
    assert "ssm:" not in policy  # it never talks to Twitch
    # Writing to raw/ would let a bug damage data that can't be collected again.
    put = [
        s
        for s in json.loads(policy)["Properties"]["PolicyDocument"]["Statement"]
        if "s3:PutObject" in s["Action"]
    ]
    assert "raw/" not in json.dumps(put)


def test_compactor_runs_daily_after_midnight_with_the_pandas_layer(template):
    template.has_resource_properties(
        "AWS::Events::Rule", {"ScheduleExpression": "cron(30 0 * * ? *)", "State": "ENABLED"}
    )
    compactor = template.find_resources(
        "AWS::Lambda::Function",
        {"Properties": {"Handler": "twitch_trends.compactor.handler.handler"}},
    )
    # The region is filled in at deploy time, so the ARN is joined from parts in the template.
    assert ":336392948345:layer:AWSSDKPandas-Python312-Arm64:32" in json.dumps(compactor)


def test_release_sync_alerts_only_when_every_attempt_fails(template):
    template.has_resource_properties("AWS::Lambda::EventInvokeConfig", {"MaximumRetryAttempts": 2})
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "MetricName": "Errors",
            "Period": 86400,
            "Threshold": 3,
            "AlarmActions": Match.any_value(),
        },
    )


def test_gaps_in_collection_are_filled_with_zero_so_the_alarm_fires(template):
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "Metrics": Match.array_with(
                [
                    Match.object_like({"Expression": "FILL(snapshots, 0)"}),
                    Match.object_like(
                        {
                            "Id": "snapshots",
                            "MetricStat": Match.object_like(
                                {
                                    "Metric": Match.object_like({"MetricName": "StreamsCollected"}),
                                    "Period": 600,
                                    "Stat": "SampleCount",
                                }
                            ),
                        }
                    ),
                ]
            ),
            "EvaluationPeriods": 3,
            "DatapointsToAlarm": 3,
            "ComparisonOperator": "LessThanThreshold",
            "AlarmActions": Match.any_value(),
        },
    )


def test_alerts_go_to_the_given_email(template):
    template.has_resource_properties(
        "AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "alerts@example.com"}
    )


def glue_table(template, name: str) -> dict:
    tables = template.find_resources(
        "AWS::Glue::Table", {"Properties": {"TableInput": {"Name": name}}}
    )
    assert len(tables) == 1
    return next(iter(tables.values()))["Properties"]["TableInput"]


@pytest.mark.parametrize("name", ["stream_snapshots", "game_daily", "snapshots", "release_games"])
def test_tables_use_partition_projection_so_no_crawler_is_needed(template, name):
    parameters = glue_table(template, name)["Parameters"]
    assert parameters["projection.enabled"] == "true"
    assert parameters["projection.dt.type"] == "date"
    assert parameters["projection.dt.range"].endswith(",NOW")


def test_table_columns_match_what_the_compactor_writes(template):
    columns = glue_table(template, "stream_snapshots")["StorageDescriptor"]["Columns"]
    assert [c["Name"] for c in columns] == STREAM_SNAPSHOTS_SCHEMA.names
    assert {"Name": "viewer_count", "Type": "int"} in columns
    assert {"Name": "snapshot_ts", "Type": "timestamp"} in columns


def test_athena_queries_are_capped_at_1_gb_and_the_cap_is_enforced(template):
    template.has_resource_properties(
        "AWS::Athena::WorkGroup",
        {
            "WorkGroupConfiguration": Match.object_like(
                {
                    "BytesScannedCutoffPerQuery": 1024**3,
                    "EnforceWorkGroupConfiguration": True,
                }
            )
        },
    )


def test_athena_results_expire_after_a_week(template):
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "LifecycleConfiguration": {
                "Rules": [
                    Match.object_like(
                        {"Prefix": "athena-results/", "ExpirationInDays": 7, "Status": "Enabled"}
                    )
                ]
            }
        },
    )


def test_saved_queries_are_published_for_every_sql_file(template):
    template.resource_count_is("AWS::Athena::NamedQuery", 3)
