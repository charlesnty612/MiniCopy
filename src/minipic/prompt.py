"""H3 prompt structure helpers.

The official H3 spec (https://huggingface.co/MiniMaxAI/MiniMax-H3) describes a
"full-reference" prompt with six sections:
  1. subject_definitions
  2. summary
  3. retention_analysis
  4. detailed_description
  5. overall_soundscape
  6. non_diegetic_music

This module provides builders for the common shapes. The user supplies plain
text; we wrap it in the right structure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Mode = Literal["t2v", "i2v", "r2v"]


@dataclass
class Subject:
    label: str           # e.g. "Subject 1"
    name: str
    description: str
    sources: list[str] = field(default_factory=list)  # e.g. ["<Picture 1>", "<Video 1>"]


@dataclass
class H3Prompt:
    """Container for a full-reference H3 prompt."""

    mode: Mode
    summary: str
    subjects: list[Subject] = field(default_factory=list)
    video_sources: list[str] = field(default_factory=list)  # "<Video 1>" labels
    retention_notes: list[str] = field(default_factory=list)
    detailed_description: str = ""
    overall_soundscape: str = ""
    non_diegetic_music: str = "N/A"
    assumptions: list[str] = field(default_factory=list)

    def render(self) -> str:
        """Return the final prompt string. ≤ 7000 chars.

        The render starts with the bracketed task-type prefix per the H3 spec;
        each section is preceded by its label on its own line.
        """
        parts: list[str] = []
        prefix = {
            "t2v": "text-to-video",
            "i2v": "image-to-video + first frame",
            "r2v": "reference generation",
        }[self.mode]
        # summary section — content starts with bracketed prefix
        parts.append(f"[{prefix}] {self.summary}".strip())

        if self.subjects:
            parts.append("\nsubject_definitions:")
            for s in self.subjects:
                src = f" Source: {', '.join(s.sources)}." if s.sources else ""
                parts.append(f"<{s.label}>: {s.name} — {s.description}.{src}")

        if self.retention_notes:
            parts.append("\nretention_analysis:")
            parts.extend(self.retention_notes)

        parts.append("\ndetailed_description:")
        parts.append(self.detailed_description)

        parts.append("\noverall_soundscape:")
        parts.append(self.overall_soundscape or "N/A")

        parts.append("\nnon_diegetic_music:")
        parts.append(self.non_diegetic_music or "N/A")

        if self.assumptions:
            parts.append("\nAssumptions:")
            parts.extend(f"- {a}" for a in self.assumptions)

        out = "\n".join(parts).strip()
        if len(out) > 7000:
            raise ValueError(f"prompt is {len(out)} chars, exceeds 7000 limit")
        return out


def quick_r2v_prompt(
    *,
    summary: str,
    detailed_description: str,
    bag_subject_label: str = "Subject 2",
    bag_subject_name: str = "the new bag",
    bag_subject_description: str = "(describe the bag here)",
    bag_picture_labels: tuple[str, ...] = ("Picture 1", "Picture 2", "Picture 3"),
    video_label: str = "Video 1",
    girl_subject_label: str = "Subject 1",
    girl_subject_name: str = "the girl",
    girl_subject_description: str = "(describe the girl here)",
    overall_soundscape: str = "ambient sound matching the scene",
    non_diegetic_music: str = "N/A",
    extra_subjects: list[Subject] | None = None,
    extra_retention: list[str] | None = None,
    assumptions: list[str] | None = None,
) -> H3Prompt:
    """Build a typical r2v (multimodal references) prompt: girl from video + bag from images."""
    subs: list[Subject] = [
        Subject(
            label=girl_subject_label,
            name=girl_subject_name,
            description=girl_subject_description,
            sources=[f"<{video_label}>"],
        ),
        Subject(
            label=bag_subject_label,
            name=bag_subject_name,
            description=bag_subject_description,
            sources=[f"<{p}>" for p in bag_picture_labels],
        ),
    ]
    if extra_subjects:
        subs.extend(extra_subjects)

    ret: list[str] = [
        f"<{girl_subject_label}>: partially_preserved — appearance, pose, and scene context are kept from <{video_label}>; the bag prop is replaced.",
        f"<{bag_subject_label}>: fully_preserved — identity, silhouette, color, pattern, hardware, and charm must match the supplied pictures across all visible angles.",
        f"<{video_label}>: partially_preserved — used as scene and composition anchor; the bag is the only prop changed.",
    ]
    if extra_retention:
        ret.extend(extra_retention)

    return H3Prompt(
        mode="r2v",
        summary=summary,
        subjects=subs,
        video_sources=[f"<{video_label}>"],
        retention_notes=ret,
        detailed_description=detailed_description,
        overall_soundscape=overall_soundscape,
        non_diegetic_music=non_diegetic_music,
        assumptions=assumptions or [],
    )
