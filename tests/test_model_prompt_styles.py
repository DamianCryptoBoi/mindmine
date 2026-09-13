"""Tests for local-model prompt adaptation."""

import random
from types import SimpleNamespace

from gas.generation.prompts.model_prompt_styles import (
    MODEL_STYLES,
    adapt_for_local_model,
)
from gas.generation.prompts.scene import SceneDescription
from gas.generation.util.prompt import truncate_prompt_if_too_long


class _WordTokenizer:
    model_max_length = 24

    def __call__(self, text, verbose=True):
        return {"input_ids": text.split()}

    def decode(self, token_ids, skip_special_tokens=True):
        return " ".join(token_ids)


def test_quality_suffixes_do_not_repolish_natural_prompts():
    polished_terms = (
        "masterpiece",
        "best quality",
        "high quality",
        "ultra quality",
        "highly detailed",
        "intricate details",
        "fine details",
        "professional photography",
        "award-winning",
        "featured on 500px",
        "artstation",
        "8k wallpaper",
        "stunning",
    )

    for model_name, config in MODEL_STYLES.items():
        if not config.quality_tags:
            continue
        for seed in range(30):
            adapted = adapt_for_local_model(
                "An ordinary unedited photograph.",
                model_name,
                is_video=False,
                rng=random.Random(seed),
            )
            prompt = adapted.prompt.lower()
            assert not any(term in prompt for term in polished_terms)


def test_scene_appropriate_realism_details_survive_local_prompt_truncation():
    prompt = (
        "amateur iPhone capture, uneven framing, slight motion blur, "
        + " ".join(f"subject{i}" for i in range(100))
    )
    adapted = adapt_for_local_model(
        prompt,
        "sdxl",
        is_video=False,
        rng=random.Random(0),
    )

    truncated = truncate_prompt_if_too_long(
        adapted.prompt,
        SimpleNamespace(tokenizer=_WordTokenizer()),
    )

    assert truncated.startswith(
        "photorealistic, ordinary real-world capture, plausible imperfections"
    )
    for detail in ("amateur iPhone capture", "uneven framing", "motion blur"):
        assert detail in truncated
    assert "sensor noise" not in truncated
    assert "automatic exposure" not in truncated


def test_negative_prompts_allow_believable_capture_content():
    intended_content = {
        "blurry",
        "low quality",
        "worst quality",
        "jpeg artifacts",
        "text",
        "logo",
        "cartoon",
        "anime",
        "illustration",
        "painting",
        "drawing",
    }

    for model_name, config in MODEL_STYLES.items():
        if not config.negative_prompt:
            continue
        adapted = adapt_for_local_model(
            "An ordinary unedited photograph.",
            model_name,
            is_video=False,
            rng=random.Random(0),
        )
        negative_terms = {
            term.strip().lower() for term in (adapted.negative_prompt or "").split(",")
        }
        assert intended_content.isdisjoint(negative_terms), model_name


def test_video_negatives_allow_retained_capture_behavior():
    intended_motion = {
        "flickering",
        "frame jumping",
        "motion blur artifacts",
        "ghosting",
        "tearing",
        "stuttering",
        "jerky motion",
        "static",
        "no motion",
    }
    sampled_terms = set()

    for seed in range(30):
        adapted = adapt_for_local_model(
            "A shaky amateur iPhone clip.",
            "animatediff",
            is_video=True,
            rng=random.Random(seed),
        )
        sampled_terms.update(
            term.strip().lower()
            for term in (adapted.negative_prompt or "").split(",")
        )

    assert intended_motion.isdisjoint(sampled_terms)


def test_landscape_negatives_allow_ordinary_messy_composition():
    intended_traits = {"bad composition", "cluttered", "messy", "unbalanced"}
    sampled_terms = set()

    for seed in range(30):
        adapted = adapt_for_local_model(
            "An ordinary unedited photograph.",
            "realvis",
            is_video=False,
            scene=SceneDescription(caption="A real vacant lot.", scene_kind="urban"),
            rng=random.Random(seed),
        )
        sampled_terms.update(
            term.strip().lower()
            for term in (adapted.negative_prompt or "").split(",")
        )

    assert intended_traits.isdisjoint(sampled_terms)
