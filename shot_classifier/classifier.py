import base64
import hashlib
import io
import os
import time

import mlflow
from langchain_community.callbacks import get_openai_callback
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from PIL import Image

from db.database import Database
from shot_classifier.models import ShotClassification
from shot_classifier.prompt import build_system_prompt

MODEL = "gpt-4o-mini"
TEMPERATURE = 0

USER_MESSAGE = """\
The image is a 5-frame horizontal contact strip sampled evenly across the clip duration \
(leftmost frame = clip start, rightmost frame = clip end).

To classify camera movement, compare the position and scale of background elements \
between the leftmost and rightmost frames:
- Pan: static background elements shift horizontally across frames
- Tilt: static background elements shift vertically across frames
- Zoom: subjects and background elements change scale (grow larger or smaller)
- Dolly/tracking: perspective changes, parallax between near and far elements
- Handheld: small random displacement and slight blur between frames
- Static: no displacement, no scale change, no blur — background is identical across all frames

Do not conclude "static" unless the background is genuinely identical in all frames. \
Classify the shot.\
"""


class ShotClassifier:

    def classify(self, md5_hash: str) -> ShotClassification:
        strip = Database().get_clip_preview(md5_hash)
        if not strip:
            raise ValueError(f"No clip preview found for {md5_hash} — generate a preview first")

        b64 = base64.b64encode(strip).decode()
        system_prompt = build_system_prompt()

        mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000"))
        mlflow.set_experiment("shot-classifier")

        with mlflow.start_run(run_name=md5_hash[:8]):
            mlflow.set_tag("md5_hash", md5_hash)
            mlflow.log_param("model", MODEL)
            mlflow.log_param("temperature", TEMPERATURE)
            mlflow.log_param("prompt_hash", hashlib.md5(system_prompt.encode()).hexdigest()[:8])

            mlflow.log_text(system_prompt, "system_prompt.txt")
            mlflow.log_text(USER_MESSAGE, "user_message.txt")
            mlflow.log_image(Image.open(io.BytesIO(strip)), "input_strip.jpg")

            t0 = time.time()
            with get_openai_callback() as cb:
                llm = ChatOpenAI(model=MODEL, temperature=TEMPERATURE)
                result = llm.with_structured_output(ShotClassification).invoke([
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=[
                        {"type": "text", "text": USER_MESSAGE},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ]),
                ])

            mlflow.log_metric("latency_s", round(time.time() - t0, 3))
            mlflow.log_metric("prompt_tokens", cb.prompt_tokens)
            mlflow.log_metric("completion_tokens", cb.completion_tokens)
            mlflow.log_metric("total_tokens", cb.total_tokens)
            mlflow.log_metric("total_cost_usd", cb.total_cost)

            mlflow.log_dict(result.model_dump(), "classification.json")

        return result
