"""Tests for minipic.prompt — H3Prompt render, quick_r2v_prompt builder."""

from __future__ import annotations

import pytest

from minipic.prompt import (
    H3Prompt,
    Mode,
    Subject,
    quick_r2v_prompt,
)


# --------------------------------------------------------------------------- Subject dataclass
class TestSubject:
    def test_label_name_description(self) -> None:
        s = Subject(label="Subject 1", name="the girl", description="young woman in a cafe")
        assert s.label == "Subject 1"
        assert s.name == "the girl"
        assert s.description == "young woman in a cafe"
        assert s.sources == []

    def test_sources_default_empty(self) -> None:
        s = Subject(label="S1", name="x", description="d")
        assert s.sources == []

    def test_sources_list(self) -> None:
        s = Subject(label="S1", name="x", description="d", sources=["<Video 1>", "<Pic 1>"])
        assert len(s.sources) == 2


# --------------------------------------------------------------------------- H3Prompt.render — structure
class TestH3PromptRender:
    def test_renders_summary_first(self) -> None:
        p = H3Prompt(mode="t2v", summary="A cat walks across a room.")
        out = p.render()
        assert out.startswith("[text-to-video] A cat walks across a room.")

    def test_i2v_prefix(self) -> None:
        p = H3Prompt(mode="i2v", summary="Image moves.")
        out = p.render()
        assert "[image-to-video" in out

    def test_r2v_prefix(self) -> None:
        p = H3Prompt(mode="r2v", summary="Video generation.")
        out = p.render()
        assert "[reference generation]" in out

    def test_subject_definitions_section(self) -> None:
        p = H3Prompt(
            mode="r2v",
            summary="x",
            subjects=[
                Subject(label="Subject 1", name="the girl", description="young", sources=["<V1>"]),
            ],
        )
        out = p.render()
        assert "subject_definitions:" in out
        assert "<Subject 1>" in out
        assert "the girl" in out
        assert "<V1>" in out

    def test_retention_analysis_section(self) -> None:
        p = H3Prompt(mode="r2v", summary="x", retention_notes=["Note 1", "Note 2"])
        out = p.render()
        assert "retention_analysis:" in out
        assert "Note 1" in out
        assert "Note 2" in out

    def test_detailed_description_section(self) -> None:
        p = H3Prompt(mode="t2v", summary="x", detailed_description="Long scene description.")
        out = p.render()
        assert "detailed_description:" in out
        assert "Long scene description." in out

    def test_overall_soundscape_section(self) -> None:
        p = H3Prompt(mode="t2v", summary="x", overall_soundscape="Soft ambient noise.")
        out = p.render()
        assert "overall_soundscape:" in out
        assert "Soft ambient noise." in out

    def test_overall_soundscape_defaults_to_na(self) -> None:
        p = H3Prompt(mode="t2v", summary="x", overall_soundscape="")
        out = p.render()
        assert "overall_soundscape:\nN/A" in out

    def test_non_diegetic_music_section(self) -> None:
        p = H3Prompt(mode="t2v", summary="x", non_diegetic_music="Upbeat pop")
        out = p.render()
        assert "non_diegetic_music:" in out
        assert "Upbeat pop" in out

    def test_non_diegetic_music_defaults_to_na(self) -> None:
        p = H3Prompt(mode="t2v", summary="x", non_diegetic_music="")
        out = p.render()
        assert "non_diegetic_music:\nN/A" in out

    def test_assumptions_section(self) -> None:
        p = H3Prompt(mode="t2v", summary="x", assumptions=["Lighting is natural.", "Shot is static."])
        out = p.render()
        assert "Assumptions:" in out
        assert "Lighting is natural." in out
        assert "Shot is static." in out

    def test_assumptions_not_included_when_empty(self) -> None:
        p = H3Prompt(mode="t2v", summary="x", assumptions=[])
        out = p.render()
        assert "Assumptions:" not in out

    def test_render_order(self) -> None:
        """Sections must appear in documented order: summary (prefix), subjects, retention, description, soundscape, music, assumptions."""
        p = H3Prompt(
            mode="t2v",
            summary="Start.",
            subjects=[Subject(label="S1", name="x", description="d")],
            retention_notes=["Retention."],
            detailed_description="Detail.",
            overall_soundscape="Sound.",
            non_diegetic_music="Music.",
            assumptions=["Assumption."],
        )
        out = p.render()
        # The summary is the first line (starts with [text-to-video] prefix, no "summary:" label)
        assert out.startswith("[text-to-video] Start.")
        # Subsequent sections appear in order
        assert out.find("subject_definitions:") < out.find("retention_analysis:")
        assert out.find("retention_analysis:") < out.find("detailed_description:")
        assert out.find("detailed_description:") < out.find("overall_soundscape:")
        assert out.find("overall_soundscape:") < out.find("non_diegetic_music:")
        assert out.find("non_diegetic_music:") < out.find("Assumptions:")


# --------------------------------------------------------------------------- H3Prompt.render — length cap
class TestH3PromptLengthCap:
    def test_strips_whitespace(self) -> None:
        p = H3Prompt(mode="t2v", summary="  Hi.  ", detailed_description="  \n  Desc  \n  ")
        out = p.render()
        assert "Hi." in out
        assert "Desc" in out

    def test_raises_when_exceeds_7000_chars(self) -> None:
        long_desc = "x" * 8000
        p = H3Prompt(mode="t2v", summary="Hi", detailed_description=long_desc)
        with pytest.raises(ValueError, match="exceeds 7000"):
            p.render()

    def test_accepts_at_7000_boundary(self) -> None:
        # Just under 7000 — should not raise
        p = H3Prompt(mode="t2v", summary="x", detailed_description="x" * 6900)
        out = p.render()
        assert len(out) <= 7000


# --------------------------------------------------------------------------- quick_r2v_prompt
class TestQuickR2vPrompt:
    def test_mode_is_r2v(self) -> None:
        p = quick_r2v_prompt(summary="s", detailed_description="d")
        assert p.mode == "r2v"

    def test_two_subjects_girl_and_bag(self) -> None:
        p = quick_r2v_prompt(summary="s", detailed_description="d")
        labels = {s.label for s in p.subjects}
        assert "Subject 1" in labels  # girl
        assert "Subject 2" in labels  # bag

    def test_girl_sources_video_label(self) -> None:
        p = quick_r2v_prompt(summary="s", detailed_description="d", video_label="Vid 1")
        girl = next(s for s in p.subjects if "Subject 1" in s.label)
        assert "<Vid 1>" in girl.sources

    def test_bag_sources_picture_labels(self) -> None:
        p = quick_r2v_prompt(
            summary="s",
            detailed_description="d",
            bag_picture_labels=("Pic 1", "Pic 2"),
        )
        bag = next(s for s in p.subjects if "Subject 2" in s.label)
        assert "<Pic 1>" in bag.sources
        assert "<Pic 2>" in bag.sources

    def test_retention_notes_contain_partially_preserved(self) -> None:
        p = quick_r2v_prompt(summary="s", detailed_description="d")
        full = "\n".join(p.retention_notes)
        assert "partially_preserved" in full
        assert "fully_preserved" in full

    def test_extra_subjects_appended(self) -> None:
        extra = Subject(label="Subject 3", name="the hat", description="red hat")
        p = quick_r2v_prompt(summary="s", detailed_description="d", extra_subjects=[extra])
        assert len(p.subjects) == 3
        assert extra.label in {s.label for s in p.subjects}

    def test_extra_retention_appended(self) -> None:
        p = quick_r2v_prompt(summary="s", detailed_description="d", extra_retention=["Keep background."])
        assert "Keep background." in p.retention_notes

    def test_custom_subject_labels(self) -> None:
        p = quick_r2v_prompt(
            summary="s",
            detailed_description="d",
            girl_subject_label="Girl",
            bag_subject_label="Bag",
            girl_subject_name="the model",
            bag_subject_name="the purse",
        )
        assert any("the model" in s.name for s in p.subjects)
        assert any("the purse" in s.name for s in p.subjects)

    def test_custom_soundscape_and_music(self) -> None:
        p = quick_r2v_prompt(
            summary="s",
            detailed_description="d",
            overall_soundscape="Jazz ambient",
            non_diegetic_music="Synthwave",
        )
        assert p.overall_soundscape == "Jazz ambient"
        assert p.non_diegetic_music == "Synthwave"

    def test_assumptions_passed(self) -> None:
        p = quick_r2v_prompt(
            summary="s",
            detailed_description="d",
            assumptions=["Assume sunny day."],
        )
        assert "Assume sunny day." in p.assumptions


# --------------------------------------------------------------------------- Mode type alias
class TestMode:
    def test_mode_literal_values(self) -> None:
        assert Mode.__args__ == ("t2v", "i2v", "r2v")  # type: ignore[attr-defined]
