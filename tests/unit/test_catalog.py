"""Unit tests for catalog module."""

import pytest
from mds.catalog import CatalogRequest, IndexEntitiesRequest, build_foundry_model


class _FakeModelInfo:
    """Minimal stand-in for Azure ML Model objects."""
    def __init__(self, name="test-model", version="1"):
        self.name = name
        self.version = version


class TestCatalogRequest:
    def test_defaults(self):
        req = CatalogRequest()
        assert req.resourceIds == []
        assert req.indexEntitiesRequest is None

    def test_with_page_size(self):
        req = CatalogRequest(indexEntitiesRequest=IndexEntitiesRequest(pageSize=10, skip=5))
        assert req.indexEntitiesRequest.pageSize == 10
        assert req.indexEntitiesRequest.skip == 5


class TestBuildFoundryModel:
    def test_basic_fields(self):
        info = _FakeModelInfo(name="mnist", version="3")
        result = build_foundry_model(info, {})

        assert result["assetId"] == "mnist-v3"
        assert result["version"] == "3"
        assert result["annotations"]["name"] == "mnist"
        assert result["properties"]["name"] == "mnist"
        assert result["properties"]["version"] == 3

    def test_tags_propagated(self):
        tags = {
            "alias": "my-alias",
            "task": "classification",
            "inputModalities": "image",
            "outputModalities": "text",
            "device": "gpu",
            "executionProvider": "cudaexecutionprovider",
            "modelType": "pytorch",
            "file_size_bytes": "12345",
            "uploaded_by": "phonepe",
            "license": "MIT",
            "licenseDescription": "Open source",
            "promptTemplate": "<|user|>{prompt}",
            "foundryLocal": "true",
            "maxOutputTokens": "2048",
            "supportsToolCalling": "true",
            "toolCallStart": "<tool_call>",
            "toolCallEnd": "</tool_call>",
        }
        result = build_foundry_model(_FakeModelInfo(), tags)
        ann_tags = result["annotations"]["tags"]

        assert ann_tags["alias"] == "my-alias"
        assert ann_tags["task"] == "classification"
        assert ann_tags["inputModalities"] == "image"
        assert ann_tags["author"] == "phonepe"
        assert ann_tags["license"] == "MIT"
        assert ann_tags["promptTemplate"] == "<|user|>{prompt}"
        # New Foundry Local optional tags
        assert ann_tags["foundryLocal"] == "true"
        assert ann_tags["maxOutputTokens"] == "2048"
        assert ann_tags["supportsToolCalling"] == "true"
        assert ann_tags["toolCallStart"] == "<tool_call>"
        assert ann_tags["toolCallEnd"] == "</tool_call>"

        variant = result["properties"]["variantInfo"]["variantMetadata"]
        assert variant["device"] == "gpu"
        assert variant["executionProvider"] == "cudaexecutionprovider"
        assert variant["modelType"] == "pytorch"
        assert variant["fileSizeBytes"] == 12345

    def test_optional_tags_omitted_when_empty(self):
        """Optional tags (tool calling, maxOutputTokens) should NOT appear when not set."""
        result = build_foundry_model(_FakeModelInfo(), {})
        ann_tags = result["annotations"]["tags"]
        assert "foundryLocal" not in ann_tags
        assert "maxOutputTokens" not in ann_tags
        assert "supportsToolCalling" not in ann_tags
        assert "toolCallStart" not in ann_tags

    def test_defaults_when_tags_empty(self):
        result = build_foundry_model(_FakeModelInfo(name="m"), {})
        ann_tags = result["annotations"]["tags"]

        assert ann_tags["alias"] == "m"
        assert ann_tags["task"] == "custom"
        assert ann_tags["author"] == "unknown"

        variant = result["properties"]["variantInfo"]["variantMetadata"]
        assert variant["device"] == "cpu"
        assert variant["modelType"] == "onnx"
        assert variant["fileSizeBytes"] == 0

    def test_non_numeric_version(self):
        info = _FakeModelInfo(version="beta")
        result = build_foundry_model(info, {})
        assert result["properties"]["version"] == 1
        assert result["properties"]["alphanumericVersion"] == "beta"
