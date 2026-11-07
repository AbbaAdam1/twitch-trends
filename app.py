"""CDK entry point. `cdk` commands run this file to find out what should exist in AWS."""

import aws_cdk as cdk

from infra.github_deploy_stack import GithubDeployStack
from infra.pipeline_stack import PipelineStack

app = cdk.App()

# Passed on the command line so a personal email address never gets committed to this public
# repo. Required, so a deploy can't silently remove the alert subscription.
alert_email = app.node.try_get_context("alert_email")
if not alert_email:
    raise ValueError("Pass your alert address: cdk deploy -c alert_email=you@example.com")

PipelineStack(app, "TwitchTrendsPipeline", alert_email=alert_email)
# Deployed by hand once: cdk deploy TwitchTrendsGithubDeploy. GitHub only deploys the pipeline.
GithubDeployStack(app, "TwitchTrendsGithubDeploy")

app.synth()
