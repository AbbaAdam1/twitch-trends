"""Defines the project's AWS resources in code. CDK turns this into a CloudFormation template."""

from pathlib import Path

import pyarrow as pa
from aws_cdk import ArnFormat, Duration, RemovalPolicy, Stack
from aws_cdk import aws_athena as athena
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cloudwatch_actions
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_glue as glue
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from constructs import Construct

from twitch_trends.collector.handler import METRIC_NAME, METRIC_NAMESPACE
from twitch_trends.common.storage import WATCHLIST_KEY
from twitch_trends.compactor.handler import (
    GAME_DAILY_SCHEMA,
    SNAPSHOTS_SCHEMA,
    STREAM_SNAPSHOTS_SCHEMA,
)

SRC_DIR = Path(__file__).parent.parent / "src"
SQL_DIR = Path(__file__).parent.parent / "sql"
CLIENT_ID_PARAM = "/twitch-trends/client-id"
CLIENT_SECRET_PARAM = "/twitch-trends/client-secret"
# AWS's free, public "AWS SDK for pandas" layer, which includes pyarrow for writing Parquet.
# pyarrow is too big to bundle with the code comfortably. The version is pinned so a new
# release can't change things without a deploy. Account 336392948345 belongs to AWS.
PANDAS_LAYER = "arn:aws:lambda:{region}:336392948345:layer:AWSSDKPandas-Python312-Arm64:32"

DATABASE_NAME = "twitch_trends"
WORKGROUP_NAME = "twitch-trends"
ATHENA_RESULTS_PREFIX = "athena-results/"
# Athena charges per byte scanned ($5 per TB). A query that would scan more than this is
# stopped, so one careless query costs at most half a cent.
MAX_BYTES_PER_QUERY = 1024**3  # 1 GB
# Partitions start here. Partition projection works out the days from this range, so new
# days need no extra step to become queryable.
FIRST_DAY = "2026-11-01"
# Parquet column types, written the way Glue and Athena name them.
GLUE_TYPES = {
    pa.string(): "string",
    pa.int32(): "int",
    pa.float64(): "double",
    pa.bool_(): "boolean",
    pa.timestamp("ms", tz="UTC"): "timestamp",
}


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
            # Athena saves every query's results as a file. They're only needed briefly.
            lifecycle_rules=[
                s3.LifecycleRule(prefix=ATHENA_RESULTS_PREFIX, expiration=Duration.days(7))
            ],
        )

        # Without an explicit log group, Lambda's logs are kept forever and slowly cost money.
        log_group = logs.LogGroup(
            self,
            "CollectorLogs",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Both functions ship the same package and differ only in which handler AWS calls.
        code = lambda_.Code.from_asset(str(SRC_DIR), exclude=["*.egg-info", "__pycache__"])
        ssm_param_arns = [
            self.format_arn(
                service="ssm",
                resource="parameter",
                resource_name=name.lstrip("/"),
                arn_format=ArnFormat.SLASH_RESOURCE_NAME,
            )
            for name in (CLIENT_ID_PARAM, CLIENT_SECRET_PARAM)
        ]

        collector = lambda_.Function(
            self,
            "Collector",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,  # cheaper per second than x86
            handler="twitch_trends.collector.handler.handler",
            code=code,
            memory_size=256,
            # A run with the watchlist took about 40 seconds in testing. The extra room covers a
            # slow day at Twitch without letting a stuck run overlap the next one.
            timeout=Duration.minutes(5),
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

        # Least privilege: the collector can only add files under raw/streams/, read the
        # watchlist and its two parameters, and publish its own metric. It can't read any
        # collected data or delete anything.
        bucket.grant_put(collector, "raw/streams/*")
        collector.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"], resources=[bucket.arn_for_objects(WATCHLIST_KEY)]
            )
        )
        # Without list permission, S3 reports a missing file as "access denied" instead of "no
        # such key", and the collector couldn't tell "no watchlist yet" from a real problem.
        # The condition limits listing to that one file name.
        collector.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket"],
                resources=[bucket.bucket_arn],
                conditions={"StringEquals": {"s3:prefix": WATCHLIST_KEY}},
            )
        )
        collector.add_to_role_policy(
            iam.PolicyStatement(actions=["ssm:GetParameter"], resources=ssm_param_arns)
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

        release_sync = lambda_.Function(
            self,
            "ReleaseSync",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="twitch_trends.release_sync.handler.handler",
            code=code,
            memory_size=256,
            timeout=Duration.minutes(5),
            log_group=logs.LogGroup(
                self,
                "ReleaseSyncLogs",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            # Unlike the collector, this runs once a day, so retrying a failure is worth it.
            # Re-running a day just overwrites that day's files.
            retry_attempts=2,
            environment={
                "BUCKET_NAME": bucket.bucket_name,
                "CLIENT_ID_PARAM": CLIENT_ID_PARAM,
                "CLIENT_SECRET_PARAM": CLIENT_SECRET_PARAM,
            },
        )
        bucket.grant_put(release_sync, "ref/*")
        release_sync.add_to_role_policy(
            iam.PolicyStatement(actions=["ssm:GetParameter"], resources=ssm_param_arns)
        )

        events.Rule(
            self,
            "ReleaseSyncSchedule",
            schedule=events.Schedule.cron(minute="0", hour="6"),  # 06:00 UTC daily
            targets=[targets.LambdaFunction(release_sync)],
        )

        compactor = lambda_.Function(
            self,
            "Compactor",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="twitch_trends.compactor.handler.handler",
            code=code,
            layers=[
                lambda_.LayerVersion.from_layer_version_arn(
                    self, "PandasLayer", PANDAS_LAYER.format(region=self.region)
                )
            ],
            # A day took about 70 seconds and 200 MB in testing. Lambda gives more CPU with
            # more memory, and parsing JSON is CPU-bound, so the extra memory makes it faster.
            # Once a day, it's still well inside the free allowance.
            memory_size=1024,
            timeout=Duration.minutes(10),
            log_group=logs.LogGroup(
                self,
                "CompactorLogs",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
            # Re-running a day replaces its files, so retrying a failure is safe.
            retry_attempts=2,
            environment={"BUCKET_NAME": bucket.bucket_name},
        )
        # It reads raw snapshots and writes only under curated/. It can't change raw data.
        compactor.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket"],
                resources=[bucket.bucket_arn],
                conditions={"StringLike": {"s3:prefix": "raw/streams/*"}},
            )
        )
        compactor.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"], resources=[bucket.arn_for_objects("raw/streams/*")]
            )
        )
        bucket.grant_put(compactor, "curated/*")
        compactor.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={"StringEquals": {"cloudwatch:namespace": METRIC_NAMESPACE}},
            )
        )

        events.Rule(
            self,
            "CompactorSchedule",
            # 00:30 UTC, compacting the day that just ended. The half hour leaves room for
            # the day's last snapshot (taken just before midnight) to finish saving.
            schedule=events.Schedule.cron(minute="30", hour="0"),
            targets=[targets.LambdaFunction(compactor)],
        )

        alerts = sns.Topic(self, "Alerts")
        alerts.add_subscription(subscriptions.EmailSubscription(alert_email))
        alert_action = cloudwatch_actions.SnsAction(alerts)

        # Fires when three 10-minute blocks in a row have no saved snapshot. A stopped collector
        # sends no data at all, and CloudWatch can keep judging an older real data point instead
        # of treating the gap as missing. A live test showed that left the alarm OK for over
        # 80 minutes. FILL turns each empty block into an explicit 0, so the gap can't hide.
        collection_stopped = cloudwatch.Alarm(
            self,
            "CollectionStopped",
            alarm_description="No Twitch snapshots saved for 30 minutes",
            metric=cloudwatch.MathExpression(
                expression="FILL(snapshots, 0)",
                using_metrics={
                    "snapshots": cloudwatch.Metric(
                        namespace=METRIC_NAMESPACE,
                        metric_name=METRIC_NAME,
                        statistic="SampleCount",
                    )
                },
                period=Duration.minutes(10),
                label="Snapshots saved",
            ),
            threshold=1,
            # Three in a row, so one run that drifts into the next block can't trigger it.
            evaluation_periods=3,
            datapoints_to_alarm=3,
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

        # The first try plus two retries means three errors in a day only when every attempt
        # failed. Until it's fixed, the collector keeps using the last good watchlist.
        release_sync_failed = cloudwatch.Alarm(
            self,
            "ReleaseSyncFailed",
            alarm_description="Daily release sync failed on every attempt",
            metric=release_sync.metric_errors(period=Duration.days(1), statistic="Sum"),
            threshold=3,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        release_sync_failed.add_alarm_action(alert_action)

        # Same rule as the release sync: three errors in a day means every attempt failed.
        compaction_failed = cloudwatch.Alarm(
            self,
            "CompactionFailed",
            alarm_description="Daily compaction failed on every attempt",
            metric=compactor.metric_errors(period=Duration.days(1), statistic="Sum"),
            threshold=3,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        compaction_failed.add_alarm_action(alert_action)

        self._add_query_layer(bucket)

    def _add_query_layer(self, bucket: s3.Bucket) -> None:
        """Tables that let Athena query the files in S3 with SQL, plus a cost-capped workgroup.

        A table here is only a description: where the files are and what columns they have.
        The data stays in S3, and nothing runs or costs money until someone queries it.
        """
        database = glue.CfnDatabase(
            self,
            "Database",
            catalog_id=self.account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(name=DATABASE_NAME),
        )

        def table(
            name: str, prefix: str, columns: list[tuple[str, str]], storage: dict
        ) -> glue.CfnTable:
            location = f"s3://{bucket.bucket_name}/{prefix}"
            created = glue.CfnTable(
                self,
                f"{name.title().replace('_', '')}Table",
                catalog_id=self.account,
                database_name=DATABASE_NAME,
                table_input=glue.CfnTable.TableInputProperty(
                    name=name,
                    table_type="EXTERNAL_TABLE",
                    partition_keys=[glue.CfnTable.ColumnProperty(name="dt", type="string")],
                    # Partition projection: Athena works out which dt= folders exist from these
                    # rules instead of a crawler scanning S3, and a WHERE on dt makes it read
                    # only those days. That's what keeps queries cheap.
                    parameters={
                        "projection.enabled": "true",
                        "projection.dt.type": "date",
                        "projection.dt.format": "yyyy-MM-dd",
                        "projection.dt.range": f"{FIRST_DAY},NOW",
                        "projection.dt.interval": "1",
                        "projection.dt.interval.unit": "DAYS",
                        "storage.location.template": location + "dt=${dt}/",
                    },
                    storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                        location=location,
                        columns=[
                            glue.CfnTable.ColumnProperty(name=col, type=col_type)
                            for col, col_type in columns
                        ],
                        **storage,
                    ),
                ),
            )
            created.node.add_dependency(database)
            return created

        parquet = {
            "input_format": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
            "output_format": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
            "serde_info": glue.CfnTable.SerdeInfoProperty(
                serialization_library="org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
            ),
        }
        for name, schema in (
            ("stream_snapshots", STREAM_SNAPSHOTS_SCHEMA),
            ("game_daily", GAME_DAILY_SCHEMA),
            ("snapshots", SNAPSHOTS_SCHEMA),
        ):
            columns = [(field.name, GLUE_TYPES[field.type]) for field in schema]
            table(name, f"curated/{name}/", columns, parquet)

        # The release sync's daily IGDB pull, so queries can join viewers to release dates.
        table(
            "release_games",
            "ref/igdb/",
            [
                ("igdb_id", "bigint"),
                ("name", "string"),
                ("first_release_date", "date"),
                ("game_type", "int"),
                ("genres", "array<string>"),
                ("hypes", "int"),
                ("twitch_game_id", "string"),
            ],
            {
                "input_format": "org.apache.hadoop.mapred.TextInputFormat",
                "output_format": ("org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"),
                "serde_info": glue.CfnTable.SerdeInfoProperty(
                    serialization_library="org.openx.data.jsonserde.JsonSerDe"
                ),
            },
        )

        workgroup = athena.CfnWorkGroup(
            self,
            "Workgroup",
            name=WORKGROUP_NAME,
            description="Queries for the twitch-trends dataset, capped at 1 GB scanned each",
            # Lets `cdk destroy` remove the workgroup even after queries have run in it.
            recursive_delete_option=True,
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                bytes_scanned_cutoff_per_query=MAX_BYTES_PER_QUERY,
                # Stops a query from overriding the cap or the results location.
                enforce_work_group_configuration=True,
                engine_version=athena.CfnWorkGroup.EngineVersionProperty(
                    selected_engine_version="Athena engine version 3"
                ),
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{bucket.bucket_name}/{ATHENA_RESULTS_PREFIX}",
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                        encryption_option="SSE_S3"
                    ),
                ),
            ),
        )

        # Saved queries show up in the Athena console, ready to run.
        for path in sorted(SQL_DIR.glob("*.sql")):
            query = athena.CfnNamedQuery(
                self,
                f"{path.stem.title().replace('_', '')}Query",
                name=path.stem,
                database=DATABASE_NAME,
                work_group=WORKGROUP_NAME,
                query_string=path.read_text(),
                description=path.read_text().splitlines()[0].removeprefix("-- "),
            )
            query.node.add_dependency(workgroup)
            query.node.add_dependency(database)
