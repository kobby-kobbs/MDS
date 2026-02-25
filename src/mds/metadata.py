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
    for key in ("max_position_embeddings", "n_positions", "max_length", "max_seq_length"):
        if key in cfg:
            tags.setdefault("maxOutputTokens", str(cfg[key])); break
    if mt in ("whisper", "wav2vec2", "hubert"):
        tags.setdefault("inputModalities", "audio"); tags.setdefault("outputModalities", "text")
    elif mt in ("vit", "resnet", "swin", "deit"):
        tags.setdefault("inputModalities", "image"); tags.setdefault("outputModalities", "text")
    else:
        tags.setdefault("inputModalities", "text"); tags.setdefault("outputModalities", "text")


def _merge_tokenizer_config(tags: dict, tok: dict):
    tc = tok.get("tokenizer_class", "")
    if tc:
        tags["tokenizerClass"] = tc
    if tok.get("chat_template"):
        tags["promptTemplate"] = tok["chat_template"]
        tags.setdefault("task", "chat-completion")
    eos = tok.get("eos_token")
    if isinstance(eos, dict):
        eos = eos.get("content", "")
    if eos:
        tags.setdefault("eosToken", str(eos))


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

