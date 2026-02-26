import io, json, logging, re, tarfile, zipfile

log = logging.getLogger("mds.metadata")

_TASK_HINTS = {
    "classification": ["softmax", "sigmoid", "classifier"],
    "object-detection": ["nms", "nonmaxsuppression", "anchors", "yolo"],
    "text-generation": ["attention", "transformer", "gpt", "llama", "qwen", "phi"],
    "chat-completion": ["attention", "transformer", "gpt", "llama", "qwen", "phi"],
    "image-segmentation": ["upsample", "convtranspose", "segmentation"],
    "speech-recognition": ["whisper", "wav2vec", "mfcc"],
}
_MODALITY_HINTS = {
    "text": ["gpt", "bert", "llama", "qwen", "phi", "-t5", "t5-", "flan", "roberta", "gemma"],
    "image": ["resnet", "mobilenet", "efficientnet", "vit-", "yolo", "mnist", "squeezenet"],
    "audio": ["whisper", "wav2vec", "hubert", "speech"],
}


def extract_onnx_metadata(content: bytes, filename: str) -> dict:
    tags = {"file_size_bytes": str(len(content))}
    lower = filename.lower()
    if lower.endswith(".zip"):
        _extract_from_zip(content, tags, lower); return tags
    if lower.endswith((".tar.gz", ".tgz", ".tar")):
        _extract_from_tar(content, tags, lower); return tags
    if lower.endswith(".onnx"):
        tags["modelType"] = "onnx"
    task = _infer_task_from_name(lower)
    if task:
        tags["task"] = task
    mod = _infer_modality(lower)
    if mod:
        tags["inputModalities"] = mod
        tags["outputModalities"] = "text" if mod == "text" else mod
    try:
        ops = _extract_onnx_op_types(content)
        if ops:
            t = _infer_task_from_ops(ops)
            if t:
                tags["task"] = t
    except Exception as e:
        log.debug(f"ONNX introspection skipped: {e}")
    return tags


def extract_metadata_from_files(file_dict: dict[str, bytes]) -> dict:
    """Extract metadata from a dict of {filename: content} (multi-file uploads).

    Works like archive extraction but on loose files. Reads config.json,
    genai_config.json, tokenizer_config.json, README.md for FL tag population.
    """
    tags: dict = {}
    fm = {name.split("/")[-1].lower(): name for name in file_dict}

    def reader(key):
        return file_dict[key]

    _process_archive_files(tags, "upload", reader=reader, file_map=fm,
                           all_names=list(file_dict.keys()))
    return tags


def _extract_from_zip(content: bytes, tags: dict, archive_name: str):
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            fm = {n.split("/")[-1].lower(): n for n in zf.namelist()}
            _process_archive_files(tags, archive_name, reader=lambda p: zf.read(p), file_map=fm, all_names=zf.namelist())
    except zipfile.BadZipFile:
        log.debug(f"Not a valid zip: {archive_name}")

def _extract_from_tar(content: bytes, tags: dict, archive_name: str):
    try:
        mode = "r:gz" if archive_name.endswith((".tar.gz", ".tgz")) else "r:"
        with tarfile.open(fileobj=io.BytesIO(content), mode=mode) as tf:
            members = [m for m in tf.getmembers() if m.isfile()]
            fm = {m.name.split("/")[-1].lower(): m.name for m in members}
            def _rd(p):
                f = tf.extractfile(p)
                return f.read() if f else b""
            _process_archive_files(tags, archive_name, reader=_rd, file_map=fm, all_names=[m.name for m in members])
    except (tarfile.TarError, EOFError):
        log.debug(f"Not a valid tar: {archive_name}")


def _process_archive_files(tags, archive_name, reader, file_map, all_names):
    if "config.json" in file_map:
        try:
            _merge_config_json(tags, json.loads(reader(file_map["config.json"])))
        except Exception as e:
            log.debug(f"config.json parse failed: {e}")
    if "genai_config.json" in file_map:
        try:
            _merge_genai_config(tags, json.loads(reader(file_map["genai_config.json"])))
        except Exception as e:
            log.debug(f"genai_config.json parse failed: {e}")
    if "generation_config.json" in file_map:
        try:
            gen_cfg = json.loads(reader(file_map["generation_config.json"]))
            max_new = gen_cfg.get("max_new_tokens")
            if max_new:
                tags["maxOutputTokens"] = str(max_new)
        except Exception as e:
            log.debug(f"generation_config.json parse failed: {e}")
    if "tokenizer_config.json" in file_map:
        try:
            _merge_tokenizer_config(tags, json.loads(reader(file_map["tokenizer_config.json"])))
        except Exception as e:
            log.debug(f"tokenizer_config.json parse failed: {e}")
    for rk in ("readme.md", "readme"):
        if rk in file_map:
            try:
                _merge_readme(tags, reader(file_map[rk]).decode("utf-8", errors="replace"))
            except Exception as e:
                log.debug(f"README parse failed: {e}")
            break
    onnx_files = [n for n in all_names if n.lower().endswith(".onnx")]
    if onnx_files:
        tags.setdefault("modelType", "onnx")
        try:
            ops = _extract_onnx_op_types(reader(onnx_files[0]))
            if ops:
                t = _infer_task_from_ops(ops)
                if t and "task" not in tags:
                    tags["task"] = t
        except Exception as e:
            log.debug(f"ONNX introspection in archive skipped: {e}")
    if "task" not in tags:
        t = _infer_task_from_name(archive_name)
        if t:
            tags["task"] = t
    if "inputModalities" not in tags:
        mod = _infer_modality(archive_name)
        if mod:
            tags["inputModalities"] = mod
            tags["outputModalities"] = "text" if mod == "text" else mod


def _merge_config_json(tags: dict, cfg: dict):
    mt = cfg.get("model_type", "")
    if mt:
        tags["modelType"] = mt
    archs = cfg.get("architectures", [])
    if archs:
        tags["architecture"] = archs[0]
    _arch_task_map = {
        "causal": "text-generation", "gpt": "text-generation",
        "seq2seq": "text2text-generation", "conditional": "text2text-generation",
        "classification": "classification", "sequenceclassification": "classification",
        "objectdetection": "object-detection", "detr": "object-detection",
        "imagesegmentation": "image-segmentation",
        "speechrecognition": "speech-recognition", "whisper": "speech-recognition",
    }
    al = (archs[0] if archs else "").lower()
    for kw, tv in _arch_task_map.items():
        if kw in al:
            tags.setdefault("task", tv); break
    # Store context length for reference but don't use it as maxOutputTokens
    # (context_length != max output tokens; FL uses maxOutputTokens for generation limits)
    for key in ("max_position_embeddings", "n_positions", "max_length", "max_seq_length"):
        if key in cfg:
            tags.setdefault("contextLength", str(cfg[key])); break
    if mt in ("whisper", "wav2vec2", "hubert"):
        tags.setdefault("inputModalities", "audio"); tags.setdefault("outputModalities", "text")
    elif mt in ("vit", "resnet", "swin", "deit"):
        tags.setdefault("inputModalities", "image"); tags.setdefault("outputModalities", "text")
    else:
        tags.setdefault("inputModalities", "text"); tags.setdefault("outputModalities", "text")


# Default max output tokens for chat models when not explicitly set.
# Context length (e.g. 40960) is NOT the same as generation output limit.
_DEFAULT_CHAT_MAX_OUTPUT = 2048


def _merge_genai_config(tags: dict, cfg: dict):
    """Extract FL-relevant metadata from genai_config.json (ONNX Runtime GenAI format)."""
    model = cfg.get("model", {})
    mt = model.get("type", "")
    if mt:
        tags.setdefault("modelType", "onnx")
    ctx = model.get("context_length")
    if ctx:
        tags["contextLength"] = str(ctx)
    # genai_config with decoder = chat/text-generation model
    # Override task (stronger signal than architecture name from config.json)
    if "decoder" in model:
        tags["task"] = "chat-completion"
    # maxOutputTokens: use max_new_tokens if set, otherwise default for chat models
    search = cfg.get("search", {})
    max_new = search.get("max_new_tokens")
    if max_new:
        tags["maxOutputTokens"] = str(max_new)
    elif "decoder" in model:
        tags.setdefault("maxOutputTokens", str(_DEFAULT_CHAT_MAX_OUTPUT))


def _merge_tokenizer_config(tags: dict, tok: dict):
    tc = tok.get("tokenizer_class", "")
    if tc:
        tags["tokenizerClass"] = tc
    # Build FL-style prompt template from chat_template or im_start/im_end tokens
    if tok.get("chat_template"):
        # FL expects a JSON prompt template, not Jinja2
        tags.setdefault("promptTemplate", _build_fl_prompt_template(tok))
        tags.setdefault("task", "chat-completion")
    eos = tok.get("eos_token")
    if isinstance(eos, dict):
        eos = eos.get("content", "")
    if eos:
        tags.setdefault("eosToken", str(eos))
    # Detect tool-calling support from special tokens
    _detect_tool_calling(tags, tok)


# ── FL-specific helpers ──────────────────────────────────────────────────

_TOOL_TOKEN_PAIRS = {
    "toolCallStart": "<tool_call>",
    "toolCallEnd": "</tool_call>",
    "toolRegisterStart": "<tools>",
    "toolRegisterEnd": "</tools>",
    "toolResponseStart": "<tool_response>",
    "toolResponseEnd": "</tool_response>",
}


def _detect_tool_calling(tags: dict, tok: dict):
    """Check tokenizer's added_tokens_decoder for tool-calling special tokens."""
    added = tok.get("added_tokens_decoder", {})
    token_contents = {v.get("content", "") for v in added.values() if isinstance(v, dict)}
    # Also check additional_special_tokens list
    extra = set(tok.get("additional_special_tokens", []))
    all_tokens = token_contents | extra
    found_any = False
    for tag_key, token_str in _TOOL_TOKEN_PAIRS.items():
        if token_str in all_tokens:
            tags.setdefault(tag_key, token_str)
            found_any = True
    if found_any:
        tags.setdefault("supportsToolCalling", "true")
        # FL expects all 6 tool tags; set register tokens as defaults even if
        # not explicitly in tokenizer (standard convention for tool-calling models)
        tags.setdefault("toolRegisterStart", "<tools>")
        tags.setdefault("toolRegisterEnd", "</tools>")


def _build_fl_prompt_template(tok: dict) -> str:
    """Build FL-style JSON prompt template from tokenizer config.

    FL expects: {"system": "<|im_start|>system\\n{Content}<|im_end|>", ...}
    """
    # Check if it uses ChatML-style im_start/im_end tokens
    added = tok.get("added_tokens_decoder", {})
    token_contents = {v.get("content", "") for v in added.values() if isinstance(v, dict)}
    if "<|im_start|>" in token_contents and "<|im_end|>" in token_contents:
        return json.dumps({
            "system": "<|im_start|>system\n{Content}<|im_end|>",
            "user": "<|im_start|>user\n{Content}<|im_end|>",
            "assistant": "<|im_start|>assistant\n{Content}<|im_end|>",
            "prompt": "<|im_start|>user\n{Content}<|im_end|>\n<|im_start|>assistant",
        })
    # Fallback: store the raw chat_template (Jinja2)
    return tok.get("chat_template", "")


def build_fl_description(model_name: str, tags: dict) -> str:
    """Generate a Foundry Local compatible model description from tags."""
    mt = tags.get("modelType", "ONNX").upper()
    device = tags.get("device", "CPU").upper()
    license_id = tags.get("license", "")
    parts = [
        f"This model is an optimized version of {model_name} to enable local inference on {device}s.",
        "",
        "# Model Description",
        f"- **Developed by:** {tags.get('author', 'Microsoft')}",
        f"- **Model type:** {mt}",
    ]
    if license_id:
        parts.append(f"- **License:** {license_id}")
    parts.append(f"- **Model Description:** This is a conversion of {model_name} for local inference on {device}s.")
    lic_desc = tags.get("licenseDescription", "")
    if lic_desc:
        parts.extend(["", f"# License", lic_desc])
    return "\n".join(parts)


def _merge_readme(tags: dict, text: str):
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return
    fm = match.group(1)
    lm = re.search(r"^license:\s*(.+)$", fm, re.MULTILINE)
    if lm:
        tags.setdefault("license", lm.group(1).strip())
    pm = re.search(r"^pipeline_tag:\s*(.+)$", fm, re.MULTILINE)
    if pm:
        tags.setdefault("task", pm.group(1).strip())


def _infer_task_from_name(filename: str) -> str | None:
    for task, kws in _TASK_HINTS.items():
        if any(kw in filename for kw in kws):
            return task
    return None

def _infer_modality(filename: str) -> str | None:
    for mod, kws in _MODALITY_HINTS.items():
        if any(kw in filename for kw in kws):
            return mod
    return None

def _infer_task_from_ops(op_types: set) -> str | None:
    lower_ops = {o.lower() for o in op_types}
    for task, kws in _TASK_HINTS.items():
        if any(kw in op for op in lower_ops for kw in kws):
            return task
    return None

_KNOWN_OPS = {
    b"Conv", b"Relu", b"BatchNormalization", b"MaxPool", b"AveragePool", b"Gemm", b"MatMul",
    b"Add", b"Mul", b"Softmax", b"Sigmoid", b"Reshape", b"Flatten", b"Concat", b"Transpose",
    b"Squeeze", b"Unsqueeze", b"Gather", b"Slice", b"Pad", b"Resize", b"ReduceMean",
    b"LayerNormalization", b"Attention", b"NonMaxSuppression", b"TopK", b"Where",
    b"ConvTranspose", b"Upsample",
}

def _extract_onnx_op_types(content: bytes) -> set:
    return {op.decode() for op in _KNOWN_OPS if op in content}

