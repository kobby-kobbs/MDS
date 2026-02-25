"""
Foundry Local SDK catalog models and response builder.
"""

import base64
import json
from typing import List, Optional

from pydantic import BaseModel


class IndexEntitiesRequest(BaseModel):
    pageSize: Optional[int] = 100
    skip: Optional[int] = None
    continuationToken: Optional[str] = None


class ResourceId(BaseModel):
    resourceId: str
    entityContainerType: str


class CatalogRequest(BaseModel):
    resourceIds: Optional[List[ResourceId]] = []
    indexEntitiesRequest: Optional[IndexEntitiesRequest] = None


class FLCatalogQuery(BaseModel):
    """Query parameters for the /catalog/fl endpoint."""
    task: Optional[str] = None          # filter by task (chat-completion, text-generation, etc.)
    device: Optional[str] = None        # filter by device (cpu, npu, gpu)
    modality: Optional[str] = None      # filter by inputModalities
    pageSize: Optional[int] = 50
    continuationToken: Optional[str] = None


# Pagination helpers


def encode_continuation_token(skip: int, page_size: int) -> str:
    """Encode pagination state into an opaque base64 token."""
    payload = json.dumps({"s": skip, "p": page_size}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_continuation_token(token: str) -> tuple[int, int]:
    """Decode a continuation token -> (skip, page_size). Returns (0, 100) on failure."""
    try:
        payload = json.loads(base64.urlsafe_b64decode(token))
        return int(payload["s"]), int(payload["p"])
    except Exception:
        return 0, 100


def build_foundry_model(model_info, tags: dict, *, registry_name: str = "") -> dict:
    """Transform Azure ML model info into Foundry Local catalog format.

    Tag fields follow the public catalog schema:
    https://learn.microsoft.com/en-us/azure/ai-foundry/foundry-local/reference/reference-catalog-api
    Reference model: qwen3-0.6b-generic-cpu in azureml registry.

    Args:
        registry_name: Azure ML registry name, used to build the azureml:// Uri
                       that FL needs for POST /openai/download.
    """
    # Core annotation tags (always present, matching FL reference exactly)
    annotation_tags: dict = {
        "alias": tags.get("alias", model_info.name),
        "author": tags.get("author", tags.get("uploaded_by", "unknown")),
        "directoryPath": tags.get("directoryPath", model_info.name),
        "disable-maap": tags.get("disable-maap", "True"),
        "foundryLocal": tags.get("foundryLocal", "true"),
        "inputModalities": tags.get("inputModalities", "text"),
        "license": tags.get("license", ""),
        "licenseDescription": tags.get("licenseDescription", ""),
        "outputModalities": tags.get("outputModalities", "text"),
        "task": tags.get("task", "chat-completion"),
    }

    # Optional tags - only include when set (mirrors public catalog behaviour)
    _optional = [
        "maxOutputTokens",
        "promptTemplate",
        "supportsToolCalling",
        "toolCallStart",
        "toolCallEnd",
        "toolRegisterStart",
        "toolRegisterEnd",
        "toolResponseStart",
        "toolResponseEnd",
    ]
    for key in _optional:
        if tags.get(key):
            annotation_tags[key] = tags[key]

    # Build azureml:// Uri for FL download (POST /openai/download)
    reg = registry_name or "azureml"
    model_uri = f"azureml://registries/{reg}/models/{model_info.name}/versions/{model_info.version}"

    entry = {
        "assetId": f"{model_info.name}-v{model_info.version}",
        "version": str(model_info.version),
        "annotations": {
            "tags": annotation_tags,
            "systemCatalogData": {
                "publisher": tags.get("publisher", "MDS"),
                "displayName": tags.get("displayName", model_info.name),
            },
            "name": model_info.name,
        },
        "properties": {
            "name": model_info.name,
            "version": int(model_info.version) if model_info.version.isdigit() else 1,
            "alphanumericVersion": str(model_info.version),
            "uri": model_uri,
            "variantInfo": {
                "parents": [],
                "variantMetadata": {
                    "modelType": tags.get("modelType", "onnx"),
                    "device": tags.get("device", "cpu"),
                    "executionProvider": tags.get("executionProvider", "cpuexecutionprovider"),
                    "fileSizeBytes": int(tags.get("file_size_bytes", 0)),
                },
            },
        },
    }
    return entry
