"""Fail-on-use AWS and agent guards for every UI and adapter test."""

import importlib
from unittest.mock import Mock

import boto3
import botocore.client
import botocore.session
import pytest

from src.agent import baseline, llm, loop, tools
from src.aws import athena


@pytest.fixture(autouse=True)
def forbid_aws_and_agent(monkeypatch, tmp_path):
    """Also detect forbidden calls swallowed by an application's error handler."""

    forbidden = Mock(side_effect=AssertionError("AWS and agent execution are forbidden"))
    for name in (
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_WEB_IDENTITY_TOKEN_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "absent-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "absent-credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    for target, names in (
        (boto3, ("client", "resource")),
        (boto3.session.Session, ("client", "resource")),
        (botocore.session.Session, ("create_client",)),
        (botocore.client.BaseClient, ("_make_api_call",)),
        (athena, ("create_athena_client", "execute_query", "execute_query_with_stats")),
        (llm, ("create_agent_llm",)),
        (llm.BedrockAgentLLM, ("converse",)),
        (loop, ("run_agent",)),
        (baseline, ("brief_case", "brief_day")),
        (tools, (
            "get_alert", "get_case_graph", "get_account_activity", "get_neighbour_scores",
            "get_related_alerts", "load_day_queue",
        )),
        (importlib.import_module("src.agent.investigate"), ("investigate", "run_agent")),
    ):
        for name in names:
            monkeypatch.setattr(target, name, forbidden)
    yield forbidden
    forbidden.assert_not_called()
