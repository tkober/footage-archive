from typing import Literal

from pydantic import BaseModel

# Visual cues for detecting each movement_type from a contact strip.
# Kept here so they stay in sync with the Literal enum above.
MOVEMENT_TYPE_CUES: dict[str, str] = {
    "static":   "background is identical across all frames — no displacement, scale change, or blur",
    "handheld": "small random displacement and slight blur between frames, no clear direction",
    "pan":      "static background elements shift horizontally between leftmost and rightmost frame",
    "tilt":     "static background elements shift vertically between leftmost and rightmost frame",
    "tracking": "camera follows a moving subject; background shifts while subject stays centred",
    "dolly":    "perspective changes with parallax between near and far elements (distinct from zoom: geometry changes, not just scale)",
    "zoom":     "subjects and background change scale uniformly without perspective or parallax change",
}


class Movement(BaseModel):
    """Camera movement of the shot."""

    movement_type: Literal["static", "handheld", "pan", "tilt", "tracking", "dolly", "zoom"]
    """Type of camera movement."""

    movement_direction: Literal["left_to_right", "right_to_left", "forward", "backward", "up", "down", "none"]
    """Dominant spatial movement direction (not for zoom)."""

    movement_intensity: Literal["none", "slow", "medium", "fast"]
    """Perceived speed of movement."""

    zoom: Literal["none", "zoom_in", "zoom_out"]
    """Optical zoom direction. Use instead of movement_direction for zoom shots."""

    zoom_intensity: Literal["none", "subtle", "medium", "strong"]
    """Strength of zoom effect."""


class Framing(BaseModel):
    """How the shot is composed."""

    shot_size: Literal["extreme_wide", "wide", "medium", "close_up", "macro"]
    """Distance to subject."""

    angle: Literal["eye_level", "low_angle", "high_angle", "top_down"]
    """Camera angle."""

    composition: Literal["centered", "rule_of_thirds", "leading_lines", "symmetry", "unknown"]
    """Visual composition style."""


class Scene(BaseModel):
    """What is happening in the shot."""

    location_type: Literal["indoor", "outdoor", "city", "nature", "transport", "unknown"]
    """Broad location category."""

    environment: str
    """Specific place (e.g. street, café, train)."""

    subjects: list[str]
    """Main visible objects or actors."""

    activity: str
    """Main action (e.g. walking, driving)."""


class Visual(BaseModel):
    """Lighting and mood."""

    time_of_day: Literal["morning", "day", "golden_hour", "night", "unknown"]
    """Apparent time of day."""

    lighting_type: Literal["natural", "artificial", "mixed", "unknown"]
    """Light source type."""

    lighting_style: Literal["soft", "harsh", "high_contrast", "low_contrast", "unknown"]
    """Quality and contrast of the light."""

    color_tone: Literal["warm", "cold", "neutral", "neon", "unknown"]
    """Overall color temperature and tone."""

    mood: str
    """Emotional tone (e.g. calm, busy, cinematic)."""


class Technical(BaseModel):
    """Optional technical metadata."""

    camera_motion_vector: Literal["left", "right", "forward", "backward", "none", "unknown"]
    """Dominant direction of camera travel in world space."""

    stability: Literal["stable", "slightly_shaky", "shaky", "unknown"]
    """Perceived camera stability."""


class ShotClassification(BaseModel):
    """Full classification of a video shot for editing purposes."""

    movement: Movement
    framing: Framing
    scene: Scene
    visual: Visual
    technical: Technical
