"""Defines the project's AWS resources in code. CDK turns this into a CloudFormation template."""

from pathlib import Path

from aws_cdk import ArnFormat, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from constructs import Construct

from twitch_trends.collector.handler import METRIC_NAME, METRIC_NAMESPACE

SRC_DIR = Path(__file__).parent.parent / "src"
CLIENT_ID_PARAM = "/twitch-trends/client-id"
CLIENT_SECRET_PARAM = "/twitch-trends/client-secret"


class PipelineStack(Stack):
    """A stack is a group of resources that get created, updated and deleted together."""

    def __init__(self, scope: Construct, construct_id: str, *, alert_email: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Raw snapshots can't be collected again, so `cdk destroy` keeps this bucket. Deleting
        # the data is a separate, deliberate step. Versioning stays off because it adds cost.
        bucket = s3.Bucket(
            self,
            "DataBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Without an explicit log group, Lambda's logs are kept forever and slowly cost money.
        log_group = logs.LogGroup(
            self,
            "CollectorLogs",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        collector = lambda_.Function(
            self,
            "Collector",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,  # cheaper per second than x86
            handler="twitch_trends.collector.handler.handler",
            code=lambda_.Code.from_asset(str(SRC_DIR), exclude=["*.egg-info", "__pycache__"]),
            memory_size=256,
            timeout=Duration.minutes(2),
            log_group=log_group,
            # The next run starts in 10 minutes anyway, so Lambda's automatic retries of a
            # failed run would only add near-duplicate snapshots.
            retry_attempts=0,
            environment={
                "BUCKET_NAME": bucket.bucket_name,
                "CLIENT_ID_PARAM": CLIENT_ID_PARAM,
                "CLIENT_SECRET_PARAM": CLIENT_SECRET_PARAM,
            },
        )

        # Least privilege: the collector can only add files under raw/streams/, read its two
        # parameters, and publish its own metric. It can't read or delete any data.
        bucket.grant_put(collector, "raw/streams/*")
        collector.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    self.format_arn(
                        service="ssm",
                        resource="parameter",
                        resource_name=name.lstrip("/"),
                        arn_format=ArnFormat.SLASH_RESOURCE_NAME,
                    )
                    for name in (CLIENT_ID_PARAM, CLIENT_SECRET_PARAM)
                ],
            )
        )
        collector.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],  # this action doesn't support specific resources
                conditions={"StringEquals": {"cloudwatch:namespace": METRIC_NAMESPACE}},
            )
        )

        events.Rule(
            self,
            "CollectorSchedule",
            schedule=events.Schedule.rate(Duration.minutes(10)),
            targets=[targets.LambdaFunction(collector, retry_attempts=0)],
        )

        alerts = sns.Topic(self, "Alerts")
        alerts.add_subscription(subscriptions.EmailSubscription(alert_email))
        alert_action = cloudwatch_actions.SnsAction(alerts)

        # Fires if no snapshot was saved for 30 minutes. Missing data counts as breaching,
        # because silence is exactly what a stopped collector looks like.
        collection_stopped = cloudwatch.Alarm(
            self,
            "CollectionStopped",
            alarm_description="No Twitch snapshots saved in the last 30 minutes",
            metric=cloudwatch.Metric(
                namespace=METRIC_NAMESPACE,
                metric_name=METRIC_NAME,
                statistic="SampleCount",
                period=Duration.minutes(30),
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
        )
        collection_stopped.add_alarm_action(alert_action)

        # A single failed run isn't worth an email, but three in an hour is a real problem.
        repeated_errors = cloudwatch.Alarm(
            self,
            "RepeatedErrors",
            alarm_description="Collector failed 3 or more times in an hour",
            metric=collector.metric_errors(period=Duration.hours(1), statistic="Sum"),
            threshold=3,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        repeated_errors.add_alarm_action(alert_action)
