"""Backfill missing Foundry Local tags on existing registry models.

Usage:
    python scripts/backfill_tags.py                     # dry-run (shows changes)
    python scripts/backfill_tags.py --apply             # apply changes
    python scripts/backfill_tags.py --apply --model X   # patch single model

Reads each model's latest version, computes missing FL-required tags with
sensible defaults, and re-registers the model with updated tags.
"""

import argparse, json, sys, os, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from azure.ai.ml import MLClient
from azure.ai.ml.entities import Model
from azure.identity import DefaultAzureCredential

REGISTRY = os.environ.get("REGISTRY_NAME", "customer-phone")

# ---- FL reference tags and default values --------------------------------

FL_DEFAULTS = {
    "alias":                lambda name, tags: tags.get("alias", name),
    "author":               lambda name, tags: tags.get("author", "Microsoft"),
    "directoryPath":        lambda name, tags: name,
    "disable-maap":         lambda name, tags: "True",
    "foundryLocal":         lambda name, tags: "true",
    "inputModalities":      lambda name, tags: _guess_modality(name, tags),
    "outputModalities":     lambda name, tags: _guess_modality(name, tags),
    "license":              lambda name, tags: "",
    "licenseDescription":   lambda name, tags: "",
    "maxOutputTokens":      lambda name, tags: "",
    "promptTemplate":       lambda name, tags: "",
    "supportsToolCalling":  lambda name, tags: "",
    "task":                 lambda name, tags: _guess_task(name, tags),
}

# Tool-calling tags -- only set when supportsToolCalling is true
TOOL_TAGS = {
    "toolCallStart":      "<tool_call>",
    "toolCallEnd":        "</tool_call>",
    "toolRegisterStart":  "<tools>",
    "toolRegisterEnd":    "</tools>",
    "toolResponseStart":  "<tool_response>",
    "toolResponseEnd":    "</tool_response>",
}


def _guess_task(name: str, tags: dict) -> str:
    """Infer task from model name or existing tags."""
    existing = tags.get("task", "")
    if existing and existing not in ("", "custom"):
        return existing
    low = name.lower()
    if any(kw in low for kw in ("qwen", "phi", "llama", "gpt", "gemma")):
        return "chat-completion"
    if any(kw in low for kw in ("mnist", "resnet", "mobilenet", "squeezenet", "googlenet", "inception", "densenet")):
        return "classification"
    return tags.get("task", "custom")


def _guess_modality(name: str, tags: dict) -> str:
    """Infer modality from model name or existing tags."""
    existing = tags.get("inputModalities", "")
    if existing:
        return existing
    low = name.lower()
    if any(kw in low for kw in ("mnist", "resnet", "mobilenet", "squeezenet", "googlenet", "inception", "densenet", "vit")):
        return "image"
    if any(kw in low for kw in ("whisper", "wav2vec", "hubert")):
        return "audio"
    return "text"


def _is_chat_model(name: str) -> bool:
    low = name.lower()
    return any(kw in low for kw in ("qwen", "phi", "llama", "gpt", "gemma", "mistral"))


def backfill_model(ml: MLClient, name: str, version: str, apply: bool) -> dict:
    """Compute and optionally apply missing tags for one model."""
    info = ml.models.get(name=name, version=version)
    tags = dict(info.tags or {})
    changes = {}

    # Apply FL defaults for missing keys
    for key, default_fn in FL_DEFAULTS.items():
        if key not in tags or tags[key] == "":
            val = default_fn(name, tags)
            if val:  # only set non-empty defaults
                changes[key] = val

    # Add tool-calling tags for chat models
    if _is_chat_model(name):
        if "supportsToolCalling" not in tags:
            changes["supportsToolCalling"] = "true"
        for tk, tv in TOOL_TAGS.items():
            if tk not in tags:
                changes[tk] = tv

    if not changes:
        return {"name": name, "version": version, "status": "up-to-date", "changes": {}}

    if not apply:
        return {"name": name, "version": version, "status": "dry-run", "changes": changes}

    # Apply changes
    new_tags = {**tags, **changes}
    # Re-register with a placeholder file (same approach as main.py)
    tmp = os.path.join(tempfile.gettempdir(), f"backfill_{name}.txt")
    with open(tmp, "w") as f:
        f.write(f"Model: {name}")
    try:
        m = Model(path=tmp, name=name, type="custom_model",
                  description=info.description or "", tags=new_tags)
        reg = ml.models.create_or_update(m)
        return {"name": name, "version": str(reg.version), "status": "updated",
                "changes": changes, "old_version": version}
    finally:
        os.unlink(tmp)


def main():
    parser = argparse.ArgumentParser(description="Backfill FL tags on registry models")
    parser.add_argument("--apply", action="store_true", help="Actually write changes (default: dry-run)")
    parser.add_argument("--model", type=str, help="Only patch this model name")
    parser.add_argument("--registry", type=str, default=REGISTRY, help=f"Registry name (default: {REGISTRY})")
    args = parser.parse_args()

    ml = MLClient(credential=DefaultAzureCredential(), registry_name=args.registry)
    models = list(ml.models.list())

    if args.model:
        models = [m for m in models if m.name == args.model]
        if not models:
            print(f"Model '{args.model}' not found in registry '{args.registry}'")
            sys.exit(1)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"\nBackfill FL Tags | Registry: {args.registry} | Models: {len(models)} | Mode: {mode}\n")
    print("=" * 80)

    results = []
    for m in models:
        try:
            result = backfill_model(ml, m.name, m.latest_version, args.apply)
            results.append(result)
            status = result["status"]
            changes = result["changes"]
            if changes:
                print(f"\n  {m.name} v{m.latest_version} -> {status}")
                for k, v in changes.items():
                    display_v = v[:60] + "..." if len(str(v)) > 60 else v
                    print(f"    + {k}: {display_v}")
            else:
                print(f"\n  {m.name} v{m.latest_version} -> up-to-date")
        except Exception as e:
            print(f"\n  {m.name} -> ERROR: {e}")
            results.append({"name": m.name, "status": "error", "error": str(e)})

    print("\n" + "=" * 80)
    updated = sum(1 for r in results if r["status"] in ("updated", "dry-run") and r.get("changes"))
    skipped = sum(1 for r in results if r["status"] == "up-to-date")
    errors = sum(1 for r in results if r["status"] == "error")
    print(f"\nSummary: {updated} to update, {skipped} up-to-date, {errors} errors")
    if not args.apply and updated > 0:
        print("Run with --apply to write changes.")


if __name__ == "__main__":
    main()
