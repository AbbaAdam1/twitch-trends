"""Checks that only this repo's main branch can deploy, and only through CDK's own roles."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from infra.github_deploy_stack import GithubDeployStack


@pytest.fixture(scope="module")
def template() -> Template:
    return Template.from_stack(GithubDeployStack(cdk.App(), "TestGithubDeploy"))


def test_github_is_trusted_through_oidc(template):
    template.has_resource_properties(
        "AWS::IAM::OIDCProvider",
        {
            "Url": "https://token.actions.githubusercontent.com",
            "ClientIdList": ["sts.amazonaws.com"],
        },
    )


def test_only_main_branch_of_this_repo_can_assume_the_role(template):
    role = next(iter(template.find_resources("AWS::IAM::Role").values()))
    trust = role["Properties"]["AssumeRolePolicyDocument"]["Statement"]
    assert len(trust) == 1
    conditions = trust[0]["Condition"]["StringEquals"]
    assert conditions["token.actions.githubusercontent.com:sub"] == (
        "repo:AbbaAdam1/twitch-trends:ref:refs/heads/main"
    )
    assert conditions["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    assert role["Properties"]["MaxSessionDuration"] == 3600


def test_role_can_only_step_into_cdk_bootstrap_roles(template):
    policies = template.find_resources("AWS::IAM::Policy")
    statements = [
        s for p in policies.values() for s in p["Properties"]["PolicyDocument"]["Statement"]
    ]
    assert [s["Action"] for s in statements] == ["sts:AssumeRole"]
    assert ":role/cdk-hnb659fds-*-" in json.dumps(statements)
    template.resource_count_is("AWS::IAM::ManagedPolicy", 0)
