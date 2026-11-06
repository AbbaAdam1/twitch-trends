"""Checks the generated AWS template, so cost and security rules can't quietly regress."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from infra.pipeline_stack import PipelineStack


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
    handlers = sorted(f["Properties"]["Handler"] for f in functions.values())
    assert handlers == [
        "twitch_trends.collector.handler.handler",
        "twitch_trends.release_sync.handler.handler",
    ]
    for function in functions.values():
        properties = function["Properties"]
        assert properties["MemorySize"] == 256
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
