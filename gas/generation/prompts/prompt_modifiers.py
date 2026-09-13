"""Modifier word lists used by the prompt composer.

Only the categories actually referenced by `model_prompt_styles.adapt_for_local_model`
are kept here:

  * ``quality_tags`` - appended for models with `quality_tags=True`
  * ``negative_video`` - sampled for video pipelines that accept negative_prompt
  * ``negative_portrait`` - sampled for portrait-kind image pipelines
  * ``negative_landscape`` - sampled for landscape-kind image pipelines

Other modifier categories from earlier iterations (style, lighting, camera,
composition, mood, ...) were removed when their consumers (`optimize_prompt`,
`enhance_prompt`, `generate_modifier_selection`) were retired. If a future
feature needs richer per-category modifiers, reintroduce them here.
"""

from __future__ import annotations

from typing import Dict, List


MODIFIERS: Dict[str, List[str]] = {
    "quality_tags": [
        "photorealistic", "natural texture", "ordinary color response",
        "unretouched detail", "subtle compression", "plausible imperfections",
        "physically coherent detail", "natural asymmetry",
    ],
    "negative_portrait": [
        "bad anatomy", "wrong anatomy", "extra limbs", "missing limbs",
        "mutated hands", "extra fingers", "missing fingers", "fused fingers",
        "deformed face",
        "crossed eyes", "dead eyes", "uncanny valley",
        "bad proportions", "long neck", "long body",
    ],
    "negative_landscape": [
        "unrealistic colors", "artificial looking", "fake", "cgi obvious",
    ],
    "negative_video": [
        "temporal inconsistency", "unnatural movement",
    ],
}
