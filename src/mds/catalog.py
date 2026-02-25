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


def build_foundry_model(model_info, tags: dict) -> dict:
    """Transform Azure ML model info into Foundry Local catalog format.

    Tag fields follow the public catalog schema:
    https://learn.microsoft.com/en-us/azure/ai-foundry/foundry-local/reference/reference-catalog-api
    Reference model: qwen2.5-7b-instruct-vitis-npu in azureml registry.
    """
    # Core annotation tags (always present)
    annotation_tags: dict = {
        "author": tags.get("uploaded_by", "unknown"),
        "alias": tags.get("alias", model_info.name),
        "directoryPath": tags.get("directoryPath", model_info.name),
        "license": tags.get("license", ""),
        "licenseDescription": tags.get("licenseDescription", ""),
        "promptTemplate": tags.get("promptTemplate", ""),
        "task": tags.get("task", "custom"),
        "inputModalities": tags.get("inputModalities", ""),
        "outputModalities": tags.get("outputModalities", ""),
    }

    # Optional tags - only include when set (mirrors public catalog behaviour)
    _optional = [
        "foundryLocal",
        "maxOutputTokens",
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

    return {
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
