"""Unit tests for metadata auto-extraction module."""

import io
import json
import tarfile
import zipfile

import pytest
from mds.metadata import (
    extract_onnx_metadata,
    _merge_config_json,
    _merge_tokenizer_config,
    _merge_readme,
    _infer_task_from_name,
    _infer_modality,
    _infer_task_from_ops,
    _extract_onnx_op_types,
    _extract_from_zip,
    _extract_from_tar,
)


class TestExtractOnnxMetadata:
    def test_onnx_file_sets_model_type(self):
        tags = extract_onnx_metadata(b"\x00" * 100, "model.onnx")
        assert tags["modelType"] == "onnx"

    def test_non_onnx_file_no_model_type(self):
        tags = extract_onnx_metadata(b"\x00" * 100, "model.bin")
        assert "modelType" not in tags

    def test_file_size_always_set(self):
        data = b"\x00" * 42
        tags = extract_onnx_metadata(data, "anything")
        assert tags["file_size_bytes"] == "42"

    def test_resnet_infers_image_modality(self):
        tags = extract_onnx_metadata(b"", "resnet50.onnx")
        assert tags.get("inputModalities") == "image"

    def test_bert_infers_text_modality(self):
        tags = extract_onnx_metadata(b"", "bert-base.onnx")
        assert tags.get("inputModalities") == "text"

    def test_whisper_infers_audio_modality(self):
        tags = extract_onnx_metadata(b"", "whisper-large.onnx")
        assert tags.get("inputModalities") == "audio"

    def test_gpt_infers_text_generation_task(self):
        tags = extract_onnx_metadata(b"", "gpt2-model.onnx")
        assert tags.get("task") in ("text-generation", "chat-completion")

    def test_yolo_infers_object_detection_task(self):
        tags = extract_onnx_metadata(b"", "yolov8-detect.onnx")
        assert tags.get("task") == "object-detection"

    def test_classifier_infers_classification_task(self):
        tags = extract_onnx_metadata(b"", "image-classifier.onnx")
        assert tags.get("task") == "classification"

    def test_mnist_infers_image(self):
        tags = extract_onnx_metadata(b"", "mnist.onnx")
        assert tags.get("inputModalities") == "image"

    def test_op_detection_softmax(self):
        """If ONNX binary contains 'Softmax' bytes, task should be classification."""
        content = b"\x00\x00Softmax\x00\x00"
        tags = extract_onnx_metadata(content, "model.onnx")
        assert tags.get("task") == "classification"

    def test_unknown_model_no_task(self):
        tags = extract_onnx_metadata(b"", "custom_model_v2.onnx")
        assert "task" not in tags

    def test_never_raises(self):
        """Should handle garbage input gracefully."""
        tags = extract_onnx_metadata(b"\xff\xfe\xfd", "bad_file.xyz")
        assert isinstance(tags, dict)
        assert "file_size_bytes" in tags


# ── Helpers for archive tests ────────────────────────────────────────

def _make_zip(file_map: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in file_map.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _make_tar_gz(file_map: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in file_map.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf.getvalue()


# ── Zip archive extraction ──────────────────────────────────────────

class TestZipArchiveExtraction:
    def test_zip_with_config_json(self):
        cfg = json.dumps({
            "model_type": "qwen2",
            "architectures": ["Qwen2ForCausalLM"],
            "max_position_embeddings": 32768,
        })
        content = _make_zip({
            "model/config.json": cfg.encode(),
            "model/model.onnx": b"\x00" * 10,
        })
        tags = extract_onnx_metadata(content, "model.zip")
        assert tags["modelType"] == "qwen2"
        assert tags["architecture"] == "Qwen2ForCausalLM"
        assert tags["task"] == "text-generation"
        assert tags["maxOutputTokens"] == "32768"

    def test_zip_with_tokenizer_config(self):
        tok = json.dumps({
            "tokenizer_class": "Qwen2Tokenizer",
            "chat_template": "{% for m in messages %}{{ m.content }}{% endfor %}",
        })
        content = _make_zip({
            "model/tokenizer_config.json": tok.encode(),
        })
        tags = extract_onnx_metadata(content, "model.zip")
        assert tags["tokenizerClass"] == "Qwen2Tokenizer"
        assert "promptTemplate" in tags
        assert tags["task"] == "chat-completion"

    def test_zip_with_readme_license(self):
        readme = "---\nlicense: apache-2.0\npipeline_tag: text-generation\n---\n# Model\nHello"
        content = _make_zip({"model/README.md": readme.encode()})
        tags = extract_onnx_metadata(content, "model.zip")
        assert tags.get("license") == "apache-2.0"
        assert tags.get("task") == "text-generation"

    def test_zip_bad_archive(self):
        tags = extract_onnx_metadata(b"not-a-zip", "fake.zip")
        assert isinstance(tags, dict)

    def test_zip_with_onnx_ops(self):
        content = _make_zip({
            "model/model.onnx": b"\x00Attention\x00MatMul\x00",
        })
        tags = extract_onnx_metadata(content, "model.zip")
        assert tags.get("modelType") == "onnx"


# ── Tar archive extraction ──────────────────────────────────────────

class TestTarArchiveExtraction:
    def test_tar_gz_with_config(self):
        cfg = json.dumps({
            "model_type": "whisper",
            "architectures": ["WhisperForConditionalGeneration"],
        })
        content = _make_tar_gz({
            "model/config.json": cfg.encode(),
            "model/model.onnx": b"\x00" * 10,
        })
        tags = extract_onnx_metadata(content, "model.tar.gz")
        assert tags["modelType"] == "whisper"
        assert tags.get("inputModalities") == "audio"

    def test_tar_bad_archive(self):
        tags = extract_onnx_metadata(b"not-a-tar", "fake.tar.gz")
        assert isinstance(tags, dict)

    def test_tgz_extension(self):
        content = _make_tar_gz({"model/file.txt": b"hello"})
        tags = extract_onnx_metadata(content, "model.tgz")
        assert isinstance(tags, dict)

    def test_plain_tar(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:") as tf:
            data = b"test"
            info = tarfile.TarInfo(name="file.txt")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        buf.seek(0)
        tags = extract_onnx_metadata(buf.getvalue(), "model.tar")
        assert isinstance(tags, dict)


# ── Config file parsers ─────────────────────────────────────────────

class TestMergeConfigJson:
    def test_causal_lm_task(self):
        tags = {}
        _merge_config_json(tags, {
            "architectures": ["GPT2LMHeadModel"],
            "model_type": "gpt2",
        })
        assert tags["task"] == "text-generation"

    def test_seq2seq_task(self):
        tags = {}
        _merge_config_json(tags, {
            "architectures": ["T5ForConditionalGeneration"],
        })
        assert tags["task"] == "text2text-generation"

    def test_classification_task(self):
        tags = {}
        _merge_config_json(tags, {
            "architectures": ["BertForSequenceClassification"],
        })
        assert tags["task"] == "classification"

    def test_object_detection_task(self):
        tags = {}
        _merge_config_json(tags, {
            "architectures": ["DetrForObjectDetection"],
        })
        assert tags["task"] == "object-detection"

    def test_image_segmentation_task(self):
        tags = {}
        _merge_config_json(tags, {
            "architectures": ["SegformerForImageSegmentation"],
        })
        assert tags["task"] == "image-segmentation"

    def test_speech_recognition_task(self):
        tags = {}
        _merge_config_json(tags, {
            "architectures": ["WhisperForSpeechRecognition"],
        })
        assert tags["task"] == "speech-recognition"

    def test_vit_image_modality(self):
        tags = {}
        _merge_config_json(tags, {"model_type": "vit"})
        assert tags.get("inputModalities") == "image"

    def test_max_position_embeddings(self):
        tags = {}
        _merge_config_json(tags, {"max_position_embeddings": 4096})
        assert tags["maxOutputTokens"] == "4096"

    def test_n_positions_fallback(self):
        tags = {}
        _merge_config_json(tags, {"n_positions": 2048})
        assert tags["maxOutputTokens"] == "2048"

    def test_no_architecture(self):
        tags = {}
        _merge_config_json(tags, {"model_type": "custom"})
        assert tags["modelType"] == "custom"


class TestMergeTokenizerConfig:
    def test_tokenizer_class(self):
        tags = {}
        _merge_tokenizer_config(tags, {"tokenizer_class": "GPT2Tokenizer"})
        assert tags["tokenizerClass"] == "GPT2Tokenizer"

    def test_chat_template_sets_task(self):
        tags = {}
        _merge_tokenizer_config(tags, {
            "chat_template": "{{ messages }}",
        })
        assert tags["task"] == "chat-completion"
        assert tags["promptTemplate"] == "{{ messages }}"

    def test_eos_token_string(self):
        tags = {}
        _merge_tokenizer_config(tags, {"eos_token": "<|endoftext|>"})
        assert tags["eosToken"] == "<|endoftext|>"

    def test_eos_token_dict(self):
        tags = {}
        _merge_tokenizer_config(tags, {"eos_token": {"content": "</s>"}})
        assert tags["eosToken"] == "</s>"


class TestMergeReadme:
    def test_extracts_license(self):
        tags = {}
        _merge_readme(tags, "---\nlicense: mit\n---\n# Model")
        assert tags["license"] == "mit"

    def test_extracts_pipeline_tag(self):
        tags = {}
        _merge_readme(tags, "---\npipeline_tag: text-generation\n---\n")
        assert tags["task"] == "text-generation"

    def test_no_frontmatter(self):
        tags = {}
        _merge_readme(tags, "# Just a readme\nNo front matter here")
        assert "license" not in tags

    def test_both_fields(self):
        tags = {}
        _merge_readme(tags, "---\nlicense: apache-2.0\npipeline_tag: chat\n---\n")
        assert tags["license"] == "apache-2.0"
        assert tags["task"] == "chat"


# ── ONNX heuristics ─────────────────────────────────────────────────

class TestOnnxOpExtraction:
    def test_detects_known_ops(self):
        content = b"\x00Conv\x00Relu\x00MatMul\x00"
        ops = _extract_onnx_op_types(content)
        assert "Conv" in ops
        assert "Relu" in ops
        assert "MatMul" in ops

    def test_empty_content(self):
        ops = _extract_onnx_op_types(b"")
        assert ops == set()


class TestInferTaskFromOps:
    def test_softmax_is_classification(self):
        assert _infer_task_from_ops({"Softmax", "MatMul"}) == "classification"

    def test_nms_is_object_detection(self):
        assert _infer_task_from_ops({"NonMaxSuppression"}) == "object-detection"

    def test_attention_is_text_gen(self):
        assert _infer_task_from_ops({"Attention"}) in ("text-generation", "chat-completion")

    def test_empty_returns_none(self):
        assert _infer_task_from_ops(set()) is None


class TestInferTaskFromName:
    def test_transformer(self):
        assert _infer_task_from_name("my-transformer-model") in ("text-generation", "chat-completion")

    def test_yolo(self):
        assert _infer_task_from_name("yolov5s.onnx") == "object-detection"

    def test_unknown(self):
        assert _infer_task_from_name("random-model.bin") is None


class TestInferModality:
    def test_resnet(self):
        assert _infer_modality("resnet50.onnx") == "image"

    def test_whisper(self):
        assert _infer_modality("whisper-large.onnx") == "audio"

    def test_gpt(self):
        assert _infer_modality("gpt2-model.onnx") == "text"

    def test_unknown(self):
        assert _infer_modality("custom.onnx") is None
