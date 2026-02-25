"""Audit registry model tags vs Foundry Local reference requirements."""

import json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from azure.ai.ml import MLClient
from azure.identity import DefaultAzureCredential

REGISTRY = os.environ.get("REGISTRY_NAME", "fl_private_model")

# All tags from the reference model qwen3-0.6b-generic-cpu
FL_REQUIRED = [
    "alias", "author", "directoryPath", "disable-maap", "foundryLocal",
    "inputModalities", "license", "licenseDescription", "maxOutputTokens",
    "outputModalities", "promptTemplate", "supportsToolCalling", "task",
]
FL_TOOL_TAGS = [
    "toolCallStart", "toolCallEnd",
    "toolRegisterStart", "toolRegisterEnd",
    "toolResponseStart", "toolResponseEnd",
]
ALL_FL_TAGS = FL_REQUIRED + FL_TOOL_TAGS

def main():
    ml = MLClient(credential=DefaultAzureCredential(), registry_name=REGISTRY)
    models = list(ml.models.list())
    print(f"\nRegistry: {REGISTRY}  |  Models: {len(models)}\n")
    print("=" * 90)

    for m in models:
        try:
            info = ml.models.get(name=m.name, version=m.latest_version)
        except Exception as e:
            print(f"  [ERROR] {m.name}: {e}")
            continue

        tags = info.tags or {}
        present = [t for t in ALL_FL_TAGS if t in tags]
        missing = [t for t in ALL_FL_TAGS if t not in tags]

        print(f"\n  {m.name}  v{info.version}")
        print(f"    Present ({len(present)}): {', '.join(present) if present else '(none)'}")
        print(f"    Missing ({len(missing)}): {', '.join(missing) if missing else '(none)'}")
        print(f"    All tags: {json.dumps(tags, indent=6)}")
        print("-" * 90)

if __name__ == "__main__":
    main()
