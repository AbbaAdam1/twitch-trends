"""Lets GitHub Actions deploy the pipeline without any AWS keys stored in GitHub.

Deployed once by hand, separately from the pipeline, so a bad pipeline change can never
lock GitHub out (or widen its own access).
"""

# How it works: on each run, GitHub gives the workflow a short-lived signed token saying which
# repo and branch it's running for. AWS checks the signature against GitHub's public keys
# (the "OIDC provider" below) and, if the conditions match, hands out credentials that expire
# within an hour. There's no long-lived password anywhere to leak or rotate.

from aws_cdk import Duration, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

GITHUB_REPO = "AbbaAdam1/twitch-trends"
# GitHub's tokens name the repo with fixed ID numbers as well as names (seen in CloudTrail on
# the first deploy). Names can be reused after a rename or deletion; the IDs can't, so a new
# repo with the same name still couldn't deploy.
GITHUB_SUBJECT_REPO = "AbbaAdam1@90100486/twitch-trends@1440908310"
DEPLOY_ROLE_NAME = "twitch-trends-github-deploy"
# CDK's bootstrap roles all start with this. "hnb659fds" is CDK's default qualifier.
CDK_ROLE_PREFIX = "cdk-hnb659fds-"


class GithubDeployStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        provider = iam.OidcProviderNative(
            self,
            "GithubOidc",
            url="https://token.actions.githubusercontent.com",
            client_ids=["sts.amazonaws.com"],
        )

        role = iam.Role(
            self,
            "DeployRole",
            role_name=DEPLOY_ROLE_NAME,
            description=f"Assumed by GitHub Actions on {GITHUB_REPO} main to run cdk deploy",
            max_session_duration=Duration.hours(1),
            assumed_by=iam.WebIdentityPrincipal(
                provider.oidc_provider_arn,
                conditions={
                    "StringEquals": {
                        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                        # Only this repo's main branch. Pull requests, other branches and
                        # forks get a different "sub" value, so they can't deploy.
                        "token.actions.githubusercontent.com:sub": (
                            f"repo:{GITHUB_SUBJECT_REPO}:ref:refs/heads/main"
                        ),
                    }
                },
            ),
        )
        # The role itself can do almost nothing. It can only step into the roles that
        # `cdk bootstrap` created, which upload the code and ask CloudFormation to deploy.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                resources=[
                    f"arn:aws:iam::{self.account}:role/{CDK_ROLE_PREFIX}*-{self.account}-{self.region}"
                ],
            )
        )
