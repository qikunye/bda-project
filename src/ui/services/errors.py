"""Safe failure descriptions; original exceptions stay in server-side objects only."""

import logging
from dataclasses import dataclass, field
from uuid import uuid4

from botocore.exceptions import ClientError, LoginRefreshRequired


@dataclass
class ServiceFailure:
    category: str
    message: str
    reference: str
    cause: Exception = field(repr=False)


def describe_failure(error: Exception, operation: str) -> ServiceFailure:
    code = error.response.get("Error", {}).get("Code", "").lower() if isinstance(error, ClientError) else ""
    name = type(error).__name__.lower()
    # Inspect messages only for classification; never return or log the raw message/response.
    text = str(error).lower()
    if isinstance(error, LoginRefreshRequired) or any(word in name for word in ("credential", "token", "profile")) or code in {
        "expiredtoken", "expiredtokenexception", "invalidclienttokenid", "unrecognizedclientexception",
        "invalidsignatureexception", "requestexpired", "authfailure",
    }:
        category = "Authentication failure"
        advice = "Refresh the AWS login for this environment, then submit the action again."
    elif code in {"accessdenied", "accessdeniedexception", "unauthorizedoperation", "forbiddenexception"} or any(
        word in text for word in ("access denied", "accessdenied", "not authorized", "insufficient permissions",
                                  "insufficient lake formation", "permission denied")
    ):
        category = "Insufficient permissions"
        advice = "Check the configured Athena/Glue, DynamoDB, S3 or Bedrock read/invoke permissions for this action."
    elif code in {"entitynotfoundexception", "resourcenotfoundexception"} or any(
        word in text for word in ("table_not_found", "schema_not_found", "database_not_found", "table does not exist")
    ):
        category = "Missing data table or resource"
        advice = "Verify the configured data tables and storage resources; the dashboard does not create them."
    elif code in {"throttlingexception", "provisionedthroughputexceededexception", "requestlimitexceeded",
                  "internalservererror", "serviceunavailable", "internalfailure"} or any(
        word in name for word in ("timeout", "connection", "unprocessed")
    ):
        category = "Transient service failure"
        advice = "The service could not complete the request. Retry with a new explicit submission."
    elif name == "incompatibleresult":
        category = "Incompatible saved records"
        advice = "Current evidence-brief data is required; old or inconsistent records cannot be displayed."
    else:
        category = "Request failed"
        advice = "The request did not complete. Existing saved evidence remains available; retry explicitly."
    reference = uuid4().hex[:12]
    logging.getLogger(__name__).warning("%s failed: %s (%s, reference %s)", operation, category,
                                        type(error).__name__, reference)
    return ServiceFailure(category, f"{operation}: {category}. {advice}", reference, error)
