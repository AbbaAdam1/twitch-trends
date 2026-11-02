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


def test_collector_is_small_and_outside_any_vpc(template):
    functions = template.find_resources(
        "AWS::Lambda::Function", {"Properties": {"Runtime": "python3.12"}}
    )
    assert len(functions) == 1
    properties = next(iter(functions.values()))["Properties"]
    assert properties["MemorySize"] == 256
    assert properties["Architectures"] == ["arm64"]
    assert "VpcConfig" not in properties  # a VPC would need a costly NAT gateway for internet


def test_no_expensive_resources(template):
    for resource_type in ("AWS::EC2::NatGateway", "AWS::Glue::Job", "AWS::Glue::Crawler"):
        template.resource_count_is(resource_type, 0)


def test_collector_permissions_are_narrow(template):
    policies = json.dumps(template.find_resources("AWS::IAM::Policy"))
    # It may write snapshots but has no way to read or delete stored data.
    assert "raw/streams/*" in policies
    assert "s3:GetObject" not in policies
    assert "s3:DeleteObject" not in policies
    assert "s3:*" not in policies
    # The only wildcard resource is the metric action, limited to one namespace.
    assert "cloudwatch:namespace" in policies
    # Exactly the two Twitch parameters, not every parameter in the account.
    assert ":parameter/twitch-trends/client-id" in policies
    assert ":parameter/twitch-trends/client-secret" in policies


def test_missing_data_triggers_the_collection_alarm(template):
    template.has_resource_properties(
        "AWS::CloudWatch::Alarm",
        {
            "MetricName": "StreamsCollected",
            "TreatMissingData": "breaching",
            "AlarmActions": Match.any_value(),
        },
    )


def test_alerts_go_to_the_given_email(template):
    template.has_resource_properties(
        "AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "alerts@example.com"}
    )
