from pathlib import Path
from typing import get_args

import yaml

from shot_classifier.models import Movement, MOVEMENT_TYPE_CUES


def build_user_message() -> str:
    movement_types = get_args(Movement.model_fields["movement_type"].annotation)
    cues = "\n".join(
        f"- {t}: {MOVEMENT_TYPE_CUES.get(t, '(see schema)')}"
        for t in movement_types
    )
    return (
        "The image is a 5-frame horizontal contact strip sampled evenly across the clip duration "
        "(leftmost frame = clip start, rightmost frame = clip end).\n\n"
        "To classify camera movement, compare the position and scale of background elements "
        "between the leftmost and rightmost frames:\n"
        f"{cues}\n\n"
        'Do not conclude "static" unless the background is genuinely identical in all frames. '
        "Classify the shot."
    )


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
