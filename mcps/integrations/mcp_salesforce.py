"""Salesforce MCP Server — use for live records, schema lookup, and validation-rule discovery during investigations."""

from __future__ import annotations

import difflib
import logging
import os
import re
import sys
from typing import Annotated, Any, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.dependencies import CurrentHeaders
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from mcps.common import bootstrap_platform_env, extract_bearer_token, require_header, validate_base_url
from shared.lib.platform_secrets import load_mcp_server_config

bootstrap_platform_env()

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)

DEFAULT_API_VERSION = "v60.0"
DEFAULT_MAX_QUERY_LIMIT = 200
DEFAULT_MAX_QUERY_FIELDS = 10


def _string_set(raw: dict[str, Any], key: str) -> frozenset[str]:
    values = raw.get(key)
    if not isinstance(values, list):
        return frozenset()
    return frozenset(str(item).strip() for item in values if str(item).strip())


def _string_map(raw: dict[str, Any], key: str) -> dict[str, str]:
    mapping = raw.get(key)
    if not isinstance(mapping, dict):
        return {}
    return {str(name): str(fields) for name, fields in mapping.items()}


# Object policy is instance-specific and loaded from platform config
# (mcps.config.salesforce). The public server ships no hardcoded org policy.
_POLICY = load_mcp_server_config(os.environ.get("PLATFORM_CONFIG_FILE", "/app/platform-config.yaml"), "salesforce")

API_VERSION = str(_POLICY.get("api_version") or DEFAULT_API_VERSION)
MAX_QUERY_LIMIT = int(_POLICY.get("max_query_limit") or DEFAULT_MAX_QUERY_LIMIT)
MAX_QUERY_FIELDS = int(_POLICY.get("max_query_fields") or DEFAULT_MAX_QUERY_FIELDS)
ALLOWED_OBJECTS: frozenset[str] = _string_set(_POLICY, "allowed_objects")
ALLOWED_TOOLING_OBJECTS: frozenset[str] = _string_set(_POLICY, "allowed_tooling_objects")
FILTER_REQUIRED_OBJECTS: frozenset[str] = _string_set(_POLICY, "filter_required_objects")
OBJECT_FIELDS: dict[str, str] = _string_map(_POLICY, "object_fields")
TOOLING_OBJECT_FIELDS: dict[str, str] = _string_map(_POLICY, "tooling_object_fields")

ID_PATTERN = re.compile(r"^[a-zA-Z0-9]{15,18}$")
FIELD_PATTERN = re.compile(r"^[a-zA-Z0-9_.]+$")
ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_DATETIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
SOQL_DATE_LITERAL_PARAM_PATTERN = re.compile(r"^(?:LAST|NEXT|N)_[A-Z_]+:\d+$")
OPERATOR_ALIASES: dict[str, str] = {
    "EQ": "=",
    "EQUAL": "=",
    "EQUALS": "=",
    "NE": "!=",
    "NOT_EQUAL": "!=",
    "NOT_EQUALS": "!=",
}


def _allowed_object_error(
    object_type: str,
    *,
    tool_name: str,
    allowed_objects: set[str] | None = None,
    hint_prefix: str | None = None,
) -> dict[str, Any]:
    allowed = sorted(allowed_objects or ALLOWED_OBJECTS)
    suggestions = difflib.get_close_matches(object_type, allowed, n=3, cutoff=0.45)
    error = {
        "error": f"Object type '{object_type}' is not allowed for {tool_name}.",
        "allowed_objects": allowed,
    }
    if suggestions:
        error["suggestions"] = suggestions
        error["hint"] = (
            hint_prefix or "Use the exact Salesforce object API name from the alert email or stack trace. "
        ) + f"Closest allowed matches: {', '.join(suggestions)}"
    else:
        error["hint"] = hint_prefix or "Use the exact Salesforce object API name from the alert email or stack trace."
    return error


def _default_fields_for_object(object_type: str) -> str:
    return OBJECT_FIELDS.get(object_type, "Id, Name, CreatedDate, LastModifiedDate")


def _default_fields_for_tooling_object(object_type: str) -> str:
    return TOOLING_OBJECT_FIELDS.get(object_type, "Id")


def _salesforce_error_hint(*, object_type: str | None = None, soql: str | None = None) -> str | None:
    hints: list[str] = []
    if object_type:
        hints.append("Use the exact object API name from the alert email. Do not guess aliases for custom objects.")
    if object_type in FILTER_REQUIRED_OBJECTS:
        hints.append(
            "Do not retry the same broad sampling query without filters. "
            "Add a concrete filter, switch to get_record when you have an ID, "
            "or stop that branch."
        )
    if object_type == "RecordType":
        hints.append(
            "When investigating record type resolution, filter RecordType by "
            "SobjectType and optionally Name or DeveloperName when you know them; "
            "do not broaden into unrelated business objects."
        )
    if soql and " Body " in f" {soql} ":
        hints.append(
            "Avoid requesting ApexClass.Body or ApexTrigger.Body until you have narrowed "
            "the class or trigger to a single exact symbol."
        )
    hints.append(
        "If a 400 persists, retry the same object with a smaller field set first, "
        "such as Id, Name, CreatedDate, LastModifiedDate."
    )
    return " ".join(hints)


def _format_salesforce_http_error(
    exc: httpx.HTTPStatusError,
    *,
    object_type: str | None = None,
    soql: str | None = None,
) -> dict[str, Any]:
    response = exc.response
    error_payload: list[dict[str, Any]] | dict[str, Any] | str | None
    try:
        error_payload = response.json()
    except ValueError:
        error_payload = response.text.strip() or None

    details: list[str] = []
    error_codes: list[str] = []
    if isinstance(error_payload, list):
        for item in error_payload:
            if isinstance(item, dict):
                message = str(item.get("message") or "").strip()
                code = str(item.get("errorCode") or "").strip()
                if message:
                    details.append(message)
                if code:
                    error_codes.append(code)
    elif isinstance(error_payload, dict):
        message = str(error_payload.get("message") or error_payload.get("error") or "").strip()
        if message:
            details.append(message)
        code = str(error_payload.get("errorCode") or "").strip()
        if code:
            error_codes.append(code)
    elif isinstance(error_payload, str) and error_payload:
        details.append(error_payload)

    error_message = details[0] if details else str(exc)
    result: dict[str, Any] = {
        "error": f"Salesforce {response.status_code}: {error_message}",
        "status_code": response.status_code,
    }
    if error_codes:
        result["error_codes"] = error_codes
    if details:
        result["details"] = details

    specific_hints: list[str] = []
    if "INVALID_TYPE" in error_codes:
        result["classification"] = "object_not_queryable_in_current_org_or_api"
        if object_type in {"ApexClass", "ApexTrigger"}:
            specific_hints.append(
                "Use query_records for ApexClass and ApexTrigger object queries, with one exact symbol filter and a small field set. "
                "If that exact standard-object query fails too, stop this branch or switch only to a Tooling-specific metadata need."
            )
        else:
            specific_hints.append(
                "This object may be visible in metadata or allowlisted by the MCP but still not queryable as a live record in this org. "
                "Switch to describe_object, list_object_fields, query_tooling_records, or alert-time evidence instead of retrying the same live-record branch."
            )
    elif "INVALID_FIELD" in error_codes:
        result["classification"] = "field_not_queryable_in_current_org_or_api"
        specific_hints.append(
            "This field is not queryable as requested. Use list_object_fields or describe_field to discover the real field API names, then retry with no more than 10 exact fields."
        )

    hint = _salesforce_error_hint(object_type=object_type, soql=soql)
    if specific_hints or hint:
        result["hint"] = " ".join(specific_hints + ([hint] if hint else []))
    return result


class FilterCondition(BaseModel):
    field: str = Field(description="Field API name")
    operator: str = Field(description="Comparison operator, for example =, !=, >, <, >=, <=, LIKE, or IN")
    value: str | list[str] | None = Field(
        description="Value to compare against. Use a string for most operators and an array for IN / NOT IN."
    )


class SortSpec(BaseModel):
    field: str = Field(description="Field API name for ORDER BY")
    direction: Literal["ASC", "DESC"] = Field(default="DESC", description="Sort direction")


mcp = FastMCP("Salesforce MCP Server")


def _sf_request(
    method: str,
    endpoint: str,
    token: str,
    *,
    api_prefix: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    if not token:
        return {"error": "Authorization header required"}

    try:
        instance_url = validate_base_url(
            require_header(
                headers=kwargs.pop("headers"),
                header_name="x-salesforce-instance-url",
                description="Salesforce instance URL",
            ),
            header_name="x-salesforce-instance-url",
        )
    except ValueError as exc:
        return {"error": str(exc)}

    url = f"{instance_url}/services/data/{API_VERSION}{api_prefix}{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=30.0, headers=headers) as client:
            if method == "POST":
                response = client.post(url, json=kwargs.get("json"))
            else:
                response = client.get(url, params=kwargs.get("params", {}))
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        logger.error("Salesforce request failed: %s %s — %s", method, url, exc)
        return _format_salesforce_http_error(
            exc,
            object_type=kwargs.get("object_type"),
            soql=kwargs.get("soql"),
        )
    except httpx.HTTPError as exc:
        logger.error("Salesforce request failed: %s %s — %s", method, url, exc)
        return {"error": str(exc)}


def _validate_id(sf_id: str) -> str | None:
    if sf_id and ID_PATTERN.match(sf_id):
        return sf_id
    return None


def _escape_soql_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _format_soql_scalar(value: Any) -> str:
    if value is None:
        return "NULL"

    stripped = str(value).strip()
    upper_value = stripped.upper()

    if upper_value in {"TRUE", "FALSE", "NULL"}:
        return upper_value
    if upper_value in {
        "YESTERDAY",
        "TODAY",
        "TOMORROW",
        "LAST_WEEK",
        "THIS_WEEK",
        "NEXT_WEEK",
        "LAST_MONTH",
        "THIS_MONTH",
        "NEXT_MONTH",
        "LAST_90_DAYS",
        "NEXT_90_DAYS",
        "LAST_QUARTER",
        "THIS_QUARTER",
        "NEXT_QUARTER",
        "LAST_YEAR",
        "THIS_YEAR",
        "NEXT_YEAR",
        "LAST_FISCAL_QUARTER",
        "THIS_FISCAL_QUARTER",
        "NEXT_FISCAL_QUARTER",
        "LAST_FISCAL_YEAR",
        "THIS_FISCAL_YEAR",
        "NEXT_FISCAL_YEAR",
    }:
        return upper_value
    if SOQL_DATE_LITERAL_PARAM_PATTERN.fullmatch(upper_value):
        return upper_value
    if ISO_DATE_PATTERN.fullmatch(stripped) or ISO_DATETIME_PATTERN.fullmatch(stripped):
        return stripped
    if re.fullmatch(r"-?\d+(?:\.\d+)?", stripped):
        return stripped

    return f"'{_escape_soql_string(stripped)}'"


def _normalize_operator(operator: str) -> str:
    normalized = str(operator or "").strip().upper().replace("-", "_").replace(" ", "_")
    normalized = OPERATOR_ALIASES.get(normalized, normalized)
    if normalized not in {"=", "!=", ">", "<", ">=", "<=", "LIKE", "IN", "NOT IN"}:
        raise ValueError(f"Invalid operator: {operator}")
    return normalized


def _parse_filter_string(filter_text: str) -> FilterCondition:
    text = str(filter_text or "").strip()
    match = re.match(
        r"^([A-Za-z0-9_.]+)\s+(NOT\s+IN|IN|LIKE|!=|>=|<=|=|>|<|EQUALS|EQUAL|EQ|NOT_EQUALS|NOT_EQUAL|NE)\s+(.+)$",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        raise ValueError(f"Invalid filter expression: {filter_text}")

    field, operator, raw_value = match.groups()
    raw_value = raw_value.strip()
    normalized_operator = _normalize_operator(operator)

    if normalized_operator in {"IN", "NOT IN"}:
        if raw_value.startswith("(") and raw_value.endswith(")"):
            raw_value = raw_value[1:-1].strip()
        values = [] if not raw_value else [item.strip().strip("'\"") for item in raw_value.split(",")]
        return FilterCondition(field=field, operator=normalized_operator, value=values)

    if raw_value.upper() == "NULL":
        parsed_value: str | None = None
    else:
        parsed_value = raw_value.strip("'\"")

    return FilterCondition(field=field, operator=normalized_operator, value=parsed_value)


def _split_filter_expressions(filter_text: str) -> list[str]:
    text = str(filter_text or "").strip()
    if not text:
        return []
    return [part.strip() for part in re.split(r"\s+AND\s+", text, flags=re.IGNORECASE) if part.strip()]


def _coerce_filters(filters: list[FilterCondition] | list[str] | str | None) -> list[FilterCondition]:
    if filters is None:
        return []
    if isinstance(filters, str):
        return [_parse_filter_string(part) for part in _split_filter_expressions(filters)]

    coerced: list[FilterCondition] = []
    for item in filters:
        if isinstance(item, FilterCondition):
            coerced.append(item)
        elif isinstance(item, str):
            coerced.extend(_parse_filter_string(part) for part in _split_filter_expressions(item))
        else:
            raise ValueError(f"Invalid filter value: {item}")
    return coerced


def _coerce_sort(sort: SortSpec | str | None) -> SortSpec | None:
    if sort is None or isinstance(sort, SortSpec):
        return sort

    text = str(sort).strip()
    if not text:
        return None

    parts = text.split()
    field = parts[0]
    direction = parts[1].upper() if len(parts) > 1 else "DESC"
    if direction not in {"ASC", "DESC"}:
        raise ValueError(f"Invalid sort direction: {direction}")
    return SortSpec(field=field, direction=direction)


def _build_filter_clause(filters: list[FilterCondition]) -> str:
    clauses: list[str] = []
    for condition in filters:
        field = condition.field
        operator = _normalize_operator(condition.operator)
        value = condition.value

        if not FIELD_PATTERN.match(field):
            raise ValueError(f"Invalid field name: {field}")

        if operator in ("IN", "NOT IN"):
            if not isinstance(value, list):
                raise ValueError(f"{operator} requires an array value")
            escaped = ", ".join(_format_soql_scalar(str(item)) for item in value)
            clauses.append(f"{field} {operator} ({escaped})")
            continue

        if isinstance(value, list):
            raise ValueError(f"{operator} requires a scalar value")

        if operator == "LIKE":
            if value is None:
                raise ValueError("LIKE requires a non-null scalar value")
            clauses.append(f"{field} LIKE '{_escape_soql_string(value)}'")
            continue

        clauses.append(f"{field} {operator} {_format_soql_scalar(value)}")

    return " AND ".join(clauses)


def _summarize_entity_definition(record: dict[str, Any]) -> dict[str, Any]:
    object_api_name = str(record.get("QualifiedApiName") or "").strip()
    return {
        "object_api_name": object_api_name,
        "label": record.get("Label"),
        "key_prefix": record.get("KeyPrefix"),
        "namespace_prefix": record.get("NamespacePrefix"),
        "is_customizable": record.get("IsCustomizable"),
        "allowed_live_record_tools": object_api_name in ALLOWED_OBJECTS,
        "allowed_tooling_metadata_tools": object_api_name in ALLOWED_TOOLING_OBJECTS,
        "supports_live_queries_in_this_org": None,
    }


def _dedupe_entity_summaries(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for record in records:
        summary = _summarize_entity_definition(record)
        key = summary["object_api_name"]
        if key and key not in deduped:
            deduped[key] = summary
    return list(deduped.values())


def _normalize_match_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _validation_rule_match_score(rule: dict[str, Any], message_text: str) -> float:
    target = _normalize_match_text(message_text)
    if not target:
        return 0.0

    best_score = 0.0
    for candidate in (
        rule.get("ErrorMessage"),
        rule.get("Description"),
        rule.get("ValidationName"),
    ):
        normalized_candidate = _normalize_match_text(candidate)
        if not normalized_candidate:
            continue
        score = difflib.SequenceMatcher(None, target, normalized_candidate).ratio()
        if target in normalized_candidate:
            score = max(score, 0.95)
        best_score = max(best_score, score)

    return round(best_score, 3)


def _describe_live_object_capability(object_api_name: str, headers: dict[str, str]) -> dict[str, Any]:
    if object_api_name not in ALLOWED_OBJECTS:
        return {
            "available": False,
            "reason": "not_allowlisted_by_mcp_live_record_tools",
        }

    result = _sf_request(
        "GET",
        f"/sobjects/{object_api_name}/describe",
        extract_bearer_token(headers),
        headers=headers,
        object_type=object_api_name,
    )
    if result.get("error"):
        return {
            "available": False,
            "error": result.get("error"),
            "error_codes": result.get("error_codes", []),
            "classification": result.get("classification"),
        }

    fields = result.get("fields", []) or []
    return {
        "available": True,
        "queryable": result.get("queryable"),
        "searchable": result.get("searchable"),
        "retrieveable": result.get("retrieveable"),
        "createable": result.get("createable"),
        "updateable": result.get("updateable"),
        "deletable": result.get("deletable"),
        "field_count": len(fields),
        "sample_fields": [field.get("name") for field in fields[:10] if field.get("name")],
    }


def _describe_live_object(object_api_name: str, headers: dict[str, str]) -> dict[str, Any]:
    return _sf_request(
        "GET",
        f"/sobjects/{object_api_name}/describe",
        extract_bearer_token(headers),
        headers=headers,
        object_type=object_api_name,
    )


def _attach_live_object_capability(summary: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    object_api_name = str(summary.get("object_api_name") or "").strip()
    if not object_api_name:
        return summary

    live_api = _describe_live_object_capability(object_api_name, headers)
    enriched = dict(summary)
    enriched["live_api"] = live_api
    enriched["supports_live_queries_in_this_org"] = bool(live_api.get("available"))
    return enriched


def _attach_live_capabilities(matches: list[dict[str, Any]], headers: dict[str, str]) -> list[dict[str, Any]]:
    enriched_matches: list[dict[str, Any]] = []
    for index, summary in enumerate(matches):
        if index >= 5:
            enriched_matches.append(summary)
            continue
        enriched_matches.append(_attach_live_object_capability(summary, headers))
    return enriched_matches


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def resolve_record_reference(
    record_id: Annotated[
        str,
        "Salesforce record ID to resolve to its most likely object type before choosing get_record or query_records.",
    ],
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Resolve a Salesforce ID prefix to the matching object metadata before you query live records."""
    valid_id = _validate_id(record_id)
    if not valid_id:
        return {"error": "Invalid Salesforce record ID format (must be 15 or 18 alphanumeric characters)"}

    key_prefix = valid_id[:3]
    result = query_tooling_records(
        object_type="EntityDefinition",
        fields=["QualifiedApiName", "Label", "KeyPrefix", "NamespacePrefix", "IsCustomizable"],
        filters=[FilterCondition(field="KeyPrefix", operator="=", value=key_prefix)],
        limit=10,
        headers=headers,
    )
    if result.get("error"):
        return result

    matches = _attach_live_capabilities(_dedupe_entity_summaries(result.get("records", [])), headers)
    response: dict[str, Any] = {
        "record_id": valid_id,
        "key_prefix": key_prefix,
        "matches": matches,
    }
    if matches:
        response["hint"] = (
            "Use get_record or query_records only when the matched object is allowed_live_record_tools=true and supports_live_queries_in_this_org is not false. "
            "Otherwise stay in metadata mode with query_tooling_records."
        )
    else:
        response["hint"] = (
            "No EntityDefinition matched this ID prefix in the current org. Use the alert text or stack trace to confirm the object before querying."
        )
    return response


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def describe_object(
    object_type: Annotated[
        str,
        "Salesforce object API name or partial object name to verify against org metadata before querying live records.",
    ],
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Check whether an object exists in this org and whether this MCP can query it live or only as metadata."""
    normalized_object = str(object_type or "").strip()
    if not normalized_object:
        return {"error": "object_type is required"}

    exact = query_tooling_records(
        object_type="EntityDefinition",
        fields=["QualifiedApiName", "Label", "KeyPrefix", "NamespacePrefix", "IsCustomizable"],
        filters=[FilterCondition(field="QualifiedApiName", operator="=", value=normalized_object)],
        limit=5,
        headers=headers,
    )
    if exact.get("error"):
        return exact

    entity_records = list(exact.get("records", []))
    if not entity_records and "%" not in normalized_object:
        wildcard = f"%{normalized_object}%"
        for field_name in ("QualifiedApiName", "Label"):
            partial = query_tooling_records(
                object_type="EntityDefinition",
                fields=["QualifiedApiName", "Label", "KeyPrefix", "NamespacePrefix", "IsCustomizable"],
                filters=[FilterCondition(field=field_name, operator="LIKE", value=wildcard)],
                limit=10,
                headers=headers,
            )
            if partial.get("error"):
                return partial
            entity_records.extend(partial.get("records", []))

    matches = _attach_live_capabilities(_dedupe_entity_summaries(entity_records), headers)
    suggestions = difflib.get_close_matches(
        normalized_object,
        sorted(ALLOWED_OBJECTS | ALLOWED_TOOLING_OBJECTS),
        n=5,
        cutoff=0.45,
    )
    response: dict[str, Any] = {
        "query": normalized_object,
        "matches": matches,
        "suggestions": suggestions,
        "hint": (
            "Use get_record/find_record/query_records for objects with allowed_live_record_tools=true and supports_live_queries_in_this_org is not false. "
            "Use query_tooling_records for setup metadata and schema inspection."
        ),
    }
    if not matches:
        response["hint"] = (
            "No object metadata matched. Use the exact API name from the alert email, record ID prefix, or stack trace instead of guessing aliases."
        )
    return response


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def describe_field(
    object_type: Annotated[str, "Exact Salesforce object API name that owns the field, such as Case or VoiceCall."],
    field_name: Annotated[
        str, "Exact or partial field API name to inspect, such as Status, UserId, or Custom_Field__c."
    ],
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Inspect field metadata when a branch depends on schema rather than a live record."""
    normalized_object = str(object_type or "").strip()
    normalized_field = str(field_name or "").strip()
    if not normalized_object:
        return {"error": "object_type is required"}
    if not normalized_field:
        return {"error": "field_name is required"}

    filters: list[FilterCondition] = [
        FilterCondition(field="EntityDefinition.QualifiedApiName", operator="=", value=normalized_object),
        FilterCondition(
            field="QualifiedApiName",
            operator="LIKE" if "%" in normalized_field else "=",
            value=normalized_field,
        ),
    ]
    result = query_tooling_records(
        object_type="FieldDefinition",
        fields=[
            "DurableId",
            "QualifiedApiName",
            "Label",
            "DataType",
            "NamespacePrefix",
            "EntityDefinition.QualifiedApiName",
        ],
        filters=filters,
        limit=20,
        headers=headers,
    )
    if result.get("error"):
        return result

    response: dict[str, Any] = {
        "object_type": normalized_object,
        "field_name": normalized_field,
        "matches": result.get("records", []),
    }
    if not response["matches"] and "%" not in normalized_field:
        response["hint"] = (
            "No exact field matched. Retry once with a partial name such as %Status% or inspect the object with describe_object first."
        )
    return response


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def list_object_fields(
    object_type: Annotated[
        str,
        "Exact Salesforce object API name whose live queryable fields you want to inspect, such as Non_MACD_Subscription_Change_Request__c, RecordType, or VoiceCall.",
    ],
    search_text: Annotated[
        str | None,
        "Optional exact or partial field-name text to narrow the result, such as Order, Status, UserId, or __c.",
    ] = None,
    limit: Annotated[int, "Maximum number of fields to return. Server capped at 100."] = 50,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """List live-queryable fields for an allowed object so you can stop guessing custom field API names."""
    normalized_object = str(object_type or "").strip()
    if not normalized_object:
        return {"error": "object_type is required"}
    if normalized_object not in ALLOWED_OBJECTS:
        return _allowed_object_error(normalized_object, tool_name="list_object_fields")

    result = _describe_live_object(normalized_object, headers)
    if result.get("error"):
        return result

    normalized_search = str(search_text or "").strip().lower()
    safe_limit = min(max(limit, 1), 100)
    fields = []
    for field in result.get("fields", []) or []:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "").strip()
        label = str(field.get("label") or "").strip()
        if not name:
            continue
        haystack = f"{name} {label}".lower()
        if normalized_search and normalized_search not in haystack:
            continue
        fields.append(
            {
                "name": name,
                "label": label or None,
                "type": field.get("type"),
                "reference_to": field.get("referenceTo") or [],
                "filterable": bool(field.get("filterable")),
                "sortable": bool(field.get("sortable")),
                "nillable": bool(field.get("nillable")),
            }
        )
        if len(fields) >= safe_limit:
            break

    response: dict[str, Any] = {
        "object_type": normalized_object,
        "search_text": search_text,
        "field_count": len(fields),
        "fields": fields,
    }
    if not fields:
        response["hint"] = (
            "No matching fields were found. Retry once with a broader partial term such as Order, Status, User, or __c."
        )
    else:
        response["hint"] = (
            "Use these exact field API names in query_records. Keep ad hoc field lists to 10 or fewer fields per query."
        )
    return response


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def find_validation_rules(
    object_type: Annotated[
        str, "Exact Salesforce object API name whose validation rules you want to inspect, such as Case or Opportunity."
    ],
    message_text: Annotated[
        str | None,
        "Optional alert validation message to rank likely rule matches. Use this when the email already contains the blocking text.",
    ] = None,
    active_only: Annotated[bool, "When true, only return active validation rules."] = True,
    limit: Annotated[int, "Maximum number of validation rules to return after ranking. Server capped at 20."] = 10,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Find likely validation rules for an object and optionally rank them by alert message text."""
    normalized_object = str(object_type or "").strip()
    if not normalized_object:
        return {"error": "object_type is required"}

    filters: list[FilterCondition] = [
        FilterCondition(field="EntityDefinition.QualifiedApiName", operator="=", value=normalized_object),
    ]
    if active_only:
        filters.append(FilterCondition(field="Active", operator="=", value="TRUE"))

    fetch_limit = min(max(limit * 5, 25), 100)
    result = query_tooling_records(
        object_type="ValidationRule",
        fields=[
            "Id",
            "ValidationName",
            "Active",
            "Description",
            "ErrorDisplayField",
            "ErrorMessage",
            "EntityDefinition.QualifiedApiName",
        ],
        filters=filters,
        sort=SortSpec(field="ValidationName", direction="ASC"),
        limit=fetch_limit,
        headers=headers,
    )
    if result.get("error"):
        return result

    rules = list(result.get("records", []))
    if message_text:
        ranked_rules = []
        for rule in rules:
            ranked_rule = dict(rule)
            ranked_rule["match_score"] = _validation_rule_match_score(rule, message_text)
            ranked_rules.append(ranked_rule)
        rules = sorted(ranked_rules, key=lambda item: item.get("match_score", 0.0), reverse=True)

    safe_limit = min(limit, 20)
    response: dict[str, Any] = {
        "object_type": normalized_object,
        "message_text": message_text,
        "searched_rules": len(rules),
        "matches": rules[:safe_limit],
    }
    if message_text:
        response["hint"] = (
            "If no strong rule match appears, treat the validation message itself as primary evidence instead of looping on broader rule scans."
        )
    return response


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def get_record(
    object_type: Annotated[
        str,
        "Live Salesforce record object to fetch by ID. Use for business data such as Case, Contact, Account, Order, Asset, Lead, User, Profile, or supported custom objects from the alert email.",
    ],
    record_id: Annotated[
        str,
        "Salesforce record ID (15 or 18 alphanumeric characters). Prefer this when the alert already gives you the concrete record ID.",
    ],
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Fetch the current state of one live record by Salesforce ID."""
    valid_id = _validate_id(record_id)
    if not valid_id:
        return {"error": "Invalid Salesforce record ID format (must be 15 or 18 alphanumeric characters)"}

    if object_type not in ALLOWED_OBJECTS:
        return _allowed_object_error(object_type, tool_name="get_record")

    fields = _default_fields_for_object(object_type)

    soql = f"SELECT {fields} FROM {object_type} WHERE Id = '{valid_id}' LIMIT 1"
    result = _sf_request(
        "GET",
        "/query",
        extract_bearer_token(headers),
        headers=headers,
        params={"q": soql},
        object_type=object_type,
        soql=soql,
    )
    records = result.get("records", [])
    if not records:
        return {"record": None, "error": f"No {object_type} found with ID {valid_id}"}
    return {"record": records[0]}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def get_case_comments(
    case_id: Annotated[str, "Salesforce Case ID (15 or 18 alphanumeric characters)."],
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Use this when case history or analyst notes may explain the current issue."""
    valid_id = _validate_id(case_id)
    if not valid_id:
        return {"error": "Invalid Salesforce Case ID format"}

    soql = (
        "SELECT Id, CommentBody, CreatedDate, CreatedBy.Name, IsPublished "
        f"FROM CaseComment WHERE ParentId = '{valid_id}' ORDER BY CreatedDate DESC LIMIT 50"
    )
    return _sf_request("GET", "/query", extract_bearer_token(headers), headers=headers, params={"q": soql})


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def query_records(
    object_type: Annotated[
        str,
        "Allowed standard Salesforce object API name to query for live business or setup records, such as Case, User, Profile, RecordType, FlowDefinitionView, ApexClass, or ApexTrigger. Use this path for ApexClass/ApexTrigger object queries with exact filters and a small field set.",
    ],
    fields: Annotated[
        list[str], "Fields to return, including relationship fields like Account.Name or UserLicense.Name."
    ],
    filters: Annotated[
        list[FilterCondition] | list[str] | str | None, "Structured WHERE conditions, or a single/simple filter string."
    ] = None,
    sort: Annotated[
        SortSpec | str | None, "Optional ORDER BY clause as a structured object or a string like 'CreatedDate DESC'."
    ] = None,
    limit: Annotated[int, "Maximum number of records to return. Server capped at 200."] = 50,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Query allowlisted live or standard objects with structured filters; raw SOQL is not accepted."""
    if object_type not in ALLOWED_OBJECTS:
        return _allowed_object_error(object_type, tool_name="query_records")
    if not fields:
        return {"error": "At least one field is required"}
    if len(fields) > MAX_QUERY_FIELDS:
        return {
            "error": f"query_records accepts at most {MAX_QUERY_FIELDS} fields per query. Use list_object_fields first, then retry with the smallest exact field set you need."
        }

    for field in fields:
        if not FIELD_PATTERN.match(field):
            return {"error": f"Invalid field name: {field}"}

    safe_limit = min(limit, MAX_QUERY_LIMIT)
    soql = f"SELECT {', '.join(fields)} FROM {object_type}"

    try:
        normalized_filters = _coerce_filters(filters)
        normalized_sort = _coerce_sort(sort)
    except ValueError as exc:
        return {"error": str(exc)}

    if object_type in FILTER_REQUIRED_OBJECTS and not normalized_filters:
        return {
            "error": (
                f"Querying {object_type} without filters is not allowed. "
                "Use get_record when you have a Salesforce ID, or add at least one filter to query_records."
            ),
            "hint": (
                "Do not repeat the same unfiltered sampling query. "
                "Add a concrete filter, use get_record for a known ID, "
                "or stop that investigation branch and summarize the evidence you already have."
            ),
        }

    if normalized_filters:
        try:
            soql += f" WHERE {_build_filter_clause(normalized_filters)}"
        except ValueError as exc:
            return {"error": str(exc)}

    if normalized_sort:
        if not FIELD_PATTERN.match(normalized_sort.field):
            return {"error": f"Invalid sort field: {normalized_sort.field}"}
        soql += f" ORDER BY {normalized_sort.field} {normalized_sort.direction}"

    soql += f" LIMIT {safe_limit}"
    return _sf_request(
        "GET",
        "/query",
        extract_bearer_token(headers),
        headers=headers,
        params={"q": soql},
        object_type=object_type,
        soql=soql,
    )


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def find_record(
    object_type: Annotated[
        str, "Allowed Salesforce object API name to search for a live record by a known identifier value."
    ],
    search_field: Annotated[str, "Field to match on, such as Email or CaseNumber."],
    search_value: Annotated[str, "Exact value to search for, or use % wildcards for LIKE matching."],
    extra_fields: Annotated[list[str] | None, "Optional extra fields to include in the result."] = None,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Use this when you have a business identifier like Email or CaseNumber but not the Salesforce ID."""
    if object_type not in ALLOWED_OBJECTS:
        return _allowed_object_error(object_type, tool_name="find_record")
    if not FIELD_PATTERN.match(search_field):
        return {"error": f"Invalid search field: {search_field}"}

    base_fields = _default_fields_for_object(object_type)
    additional_fields = extra_fields or []
    for field in additional_fields:
        if not FIELD_PATTERN.match(field):
            return {"error": f"Invalid extra field: {field}"}
    if additional_fields:
        base_fields = f"{base_fields}, {', '.join(additional_fields)}"

    escaped_value = _escape_soql_string(search_value)
    comparator = "LIKE" if "%" in search_value else "="
    soql = f"SELECT {base_fields} FROM {object_type} WHERE {search_field} {comparator} '{escaped_value}' LIMIT 10"
    return _sf_request(
        "GET",
        "/query",
        extract_bearer_token(headers),
        headers=headers,
        params={"q": soql},
        object_type=object_type,
        soql=soql,
    )


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
def query_tooling_records(
    object_type: Annotated[
        str,
        "Salesforce Tooling API object to query for setup metadata. Use this for ValidationRule, EntityDefinition, FieldDefinition, Flow, FlowDefinition, WorkflowRule, or metadata-oriented RecordType/Profile inspection. Do not use generic guesses like CustomObject here.",
    ],
    fields: Annotated[
        list[str] | None, "Optional fields to return. Omit to use a safe predefined field set for the tooling object."
    ] = None,
    filters: Annotated[
        list[FilterCondition] | list[str] | str | None, "Structured WHERE conditions, or a single/simple filter string."
    ] = None,
    sort: Annotated[
        SortSpec | str | None,
        "Optional ORDER BY clause as a structured object or a string like 'LastModifiedDate DESC'.",
    ] = None,
    limit: Annotated[int, "Maximum number of records to return. Server capped at 200."] = 25,
    headers: dict[str, str] = CurrentHeaders(),
) -> dict[str, Any]:
    """Query allowlisted Tooling API metadata for schema and automation inspection."""
    if object_type not in ALLOWED_TOOLING_OBJECTS:
        return _allowed_object_error(
            object_type,
            tool_name="query_tooling_records",
            allowed_objects=ALLOWED_TOOLING_OBJECTS,
            hint_prefix=(
                "Use query_records/get_record for live business records. For object discovery, use describe_object or EntityDefinition. "
                "Use exact Tooling API object names such as ValidationRule, EntityDefinition, FieldDefinition, Flow, FlowDefinition, or WorkflowRule. "
            ),
        )

    selected_fields = fields or [field.strip() for field in _default_fields_for_tooling_object(object_type).split(",")]
    if not selected_fields:
        return {"error": "At least one field is required"}
    if len(selected_fields) > MAX_QUERY_FIELDS:
        return {
            "error": f"query_tooling_records accepts at most {MAX_QUERY_FIELDS} fields per query. Retry with the smallest exact metadata field set you need."
        }

    for field in selected_fields:
        if not FIELD_PATTERN.match(field):
            return {"error": f"Invalid field name: {field}"}

    safe_limit = min(limit, MAX_QUERY_LIMIT)
    soql = f"SELECT {', '.join(selected_fields)} FROM {object_type}"

    try:
        normalized_filters = _coerce_filters(filters)
        normalized_sort = _coerce_sort(sort)
    except ValueError as exc:
        return {"error": str(exc)}

    if normalized_filters:
        try:
            soql += f" WHERE {_build_filter_clause(normalized_filters)}"
        except ValueError as exc:
            return {"error": str(exc)}

    if normalized_sort:
        if not FIELD_PATTERN.match(normalized_sort.field):
            return {"error": f"Invalid sort field: {normalized_sort.field}"}
        soql += f" ORDER BY {normalized_sort.field} {normalized_sort.direction}"

    soql += f" LIMIT {safe_limit}"
    return _sf_request(
        "GET",
        "/query",
        extract_bearer_token(headers),
        api_prefix="/tooling",
        headers=headers,
        params={"q": soql},
        object_type=object_type,
        soql=soql,
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(_request):
    return JSONResponse({"status": "ok", "service": "mcp-salesforce"})


app = mcp.http_app(path="/mcp", transport="streamable-http", stateless_http=False)
