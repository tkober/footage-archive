from pathlib import Path

import yaml


def build_system_prompt() -> str:
    schema_path = Path(__file__).parent / "shots.yml"
    schema = yaml.safe_load(schema_path.read_text())
    lines = [schema["description"].strip(), "", "Fields and allowed values:"]
    for section, content in schema["fields"].items():
        lines.append(f"\n[{section}] {content['description']}")
        for field, spec in content.items():
            if field == "description":
                continue
            if isinstance(spec, dict) and "values" in spec:
                lines.append(f"  {field}: {spec['values']}  — {spec.get('description', '')}")
            elif isinstance(spec, dict):
                lines.append(f"  {field}: {spec.get('type', '')}  — {spec.get('description', '')}")
    return "\n".join(lines)
