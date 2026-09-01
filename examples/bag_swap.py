"""One-click H3 video generation: brief -> Context-IR -> generate -> download.

Workflow:
  1. Upload local reference files via V1 file API
  2. Send brief + reference URLs to H3-Context-IR -> get a structured 6-section H3 prompt
  3. Use that prompt in a H3 generation call (with the same references)
  4. Poll until done, download MP4

Run from inside the project venv:

    cd D:\\zcodeproject\\minipic
    .venv\\Scripts\\python.exe examples\\bag_swap.py

Edit the `BRIEF` and reference file paths below, or override with --brief / --ref-* flags.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from minipic.client import MiniMaxClient  # noqa: E402
from minipic.config import load_config, require_api_key  # noqa: E402
from minipic.media import prepare_reference_video, resolve_reference  # noqa: E402
from minipic.poller import poll_until_done  # noqa: E402


# ============================================================ DEFAULT INPUTS

# A simple Chinese brief — Context-IR understands Chinese and will rewrite it
# into a structured English H3 prompt. The original reference video anchors the
# scene; the bag images define the new bag identity.
DEFAULT_BRIEF = (
    "高铁车厢里一个穿校服的女孩低头看手机，"
    "把背的白蓝卡通书包换成我们粉色 Hello Kitty tote 手提包，"
    "9:16 竖屏，10 秒，镜头从背部慢推到包再拉开。"
)

# Reference media — replace these with your own file paths (or pass https:// URLs).
HERE = Path(__file__).resolve().parent
REFERENCE_VIDEO = HERE / "girl_on_train.mp4"      # scene anchor (the original video)
BAG_FRONT = HERE / "bag_front.jpg"
BAG_SIDE = HERE / "bag_side.jpg"
BAG_BACK = HERE / "bag_back.png"

# Output
OUTPUT_DIR = ROOT / "videos"
PROMPT_CACHE = HERE / "bag_swap_prompt.txt"   # last Context-IR output (overwritten each run)


# ============================================================ HELPERS

async def _upload_reference(client: MiniMaxClient, source: str | Path) -> str:
    """Resolve a local path or URL to a public URL. Caches via media.resolve_reference."""
    return await resolve_reference(client, source)


async def _prepare_video_clips(client: MiniMaxClient, source: str | Path) -> list[Path]:
    """Split a long video into ≤3 × 5s clips; return the list of clip paths."""
    if isinstance(source, str) and (source.startswith("http://") or source.startswith("https://")):
        return [Path(source)]  # URL — no local split
    # prepare_reference_video is `async def`; just await it directly.
    # (Wrapping in asyncio.to_thread would return a coroutine object, not a list.)
    return [c.path for c in await prepare_reference_video(Path(source))]


def _content_for_context_ir(brief: str, ref_image_urls: list[str], ref_video_urls: list[str]) -> list[dict]:
    """Build the content[] for the Context-IR call.

    Per MiniMax docs, Context-IR accepts the same multimodal content[] as
    video generation, so we can use the same reference URLs that will be
    used in the generation step.
    """
    content: list[dict] = [{"type": "text", "text": brief}]
    for url in ref_image_urls:
        content.append({"type": "image_url", "url": url, "role": "reference_image"})
    for url in ref_video_urls:
        content.append({"type": "video_url", "url": url, "role": "reference_video"})
    return content


def _content_for_generate(enhanced_prompt: str, ref_image_urls: list[str], ref_video_urls: list[str]) -> list[dict]:
    """Build the content[] for the generation call, using the Context-IR output as text."""
    content: list[dict] = [{"type": "text", "text": enhanced_prompt}]
    for url in ref_image_urls:
        content.append({"type": "image_url", "url": url, "role": "reference_image"})
    for url in ref_video_urls:
        content.append({"type": "video_url", "url": url, "role": "reference_video"})
    return content


def _print_enhanced_prompt(prompt: str) -> None:
    print("\n" + "=" * 72)
    print(f"Context-IR enhanced prompt ({len(prompt)} chars):")
    print("=" * 72)
    print(prompt[:1200] + ("\n..." if len(prompt) > 1200 else ""))
    print("=" * 72 + "\n")


# ============================================================ MAIN

async def amain(args: argparse.Namespace) -> int:
    cfg = load_config()
    api_key = require_api_key(cfg)
    print(f"using base_url={cfg.base_url}  api_key=***{api_key[-4:]}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ---- 1. Resolve reference media (upload local files; URLs pass through)
    ref_image_paths: list[Path] = []
    if args.ref_image:
        ref_image_paths = [Path(p) for p in args.ref_image]
    elif BAG_FRONT.is_file() and BAG_SIDE.is_file() and BAG_BACK.is_file():
        ref_image_paths = [BAG_FRONT, BAG_SIDE, BAG_BACK]
    else:
        print("ERROR: no --ref-image given and the default bag_* files are missing.",
              file=sys.stderr)
        return 2

    ref_video_path: Optional[Path] = None
    if args.ref_video:
        ref_video_path = Path(args.ref_video[0])
    elif REFERENCE_VIDEO.is_file():
        ref_video_path = REFERENCE_VIDEO
    else:
        print("WARN: no --ref-video given; running without scene-anchor video.",
              file=sys.stderr)
        ref_video_path = None

    duration = args.duration
    ratio = args.ratio
    resolution = args.resolution

    async with MiniMaxClient(cfg) as client:
        # Upload
        print("uploading reference media...")
        ref_image_urls: list[str] = []
        for p in ref_image_paths:
            url = await _upload_reference(client, p)
            ref_image_urls.append(url)
            print(f"  {p.name} -> uploaded")

        ref_video_urls: list[str] = []
        if ref_video_path is not None:
            clip_paths = await _prepare_video_clips(client, ref_video_path)
            print(f"  {ref_video_path.name} -> {len(clip_paths)} clip(s)")
            for clip in clip_paths:
                url = await _upload_reference(client, clip)
                ref_video_urls.append(url)

        # ---- 2. Context-IR: brief + references -> structured prompt
        print("\nstep 1/2: Context-IR rewriting brief into H3 6-section prompt...")
        ir_content = _content_for_context_ir(args.brief, ref_image_urls, ref_video_urls)
        ir_task_id = await client.create_context_ir_task(
            model="MiniMax-H3", content=ir_content, duration=duration, ratio=ratio,
        )
        print(f"  context-ir task_id = {ir_task_id}")
        enhanced_prompt = await client.fetch_context_ir_prompt(ir_task_id)
        _print_enhanced_prompt(enhanced_prompt)
        PROMPT_CACHE.write_text(enhanced_prompt, encoding="utf-8")
        print(f"saved enhanced prompt to {PROMPT_CACHE}")

        if len(enhanced_prompt) > 7000:
            print(f"ERROR: enhanced prompt is {len(enhanced_prompt)} chars, exceeds 7000",
                  file=sys.stderr)
            return 3

        # ---- 3. Generate: enhanced prompt + same references
        print("step 2/2: H3 video generation...")
        gen_content = _content_for_generate(enhanced_prompt, ref_image_urls, ref_video_urls)
        gen_task_id = await client.create_video_task(
            model="MiniMax-H3", content=gen_content,
            duration=duration, resolution=resolution, ratio=ratio,
        )
        print(f"  task_id = {gen_task_id}")
        final = await poll_until_done(
            client, gen_task_id,
            interval_seconds=cfg.poll_interval_seconds,
            on_progress=lambda s: print(f"  status: {s}"),
        )

        # ---- 4. Download
        content_url = ((final.get("content") or [{}])[0]).get("url")
        if not content_url:
            print("ERROR: succeeded but no content url in response", file=sys.stderr)
            print(json.dumps(final, indent=2, ensure_ascii=False))
            return 4

        out_path = OUTPUT_DIR / f"{gen_task_id}.mp4"
        tmp = out_path.with_suffix(".mp4.part")
        print(f"downloading to {out_path}...")
        await client.download_video(content_url, tmp)
        tmp.replace(out_path)
        print(f"done: {out_path}  ({out_path.stat().st_size / (1024*1024):.2f} MB)")
        return 0


def main() -> int:
    p = argparse.ArgumentParser(description="One-click H3 video generation via Context-IR")
    p.add_argument("--brief", default=DEFAULT_BRIEF,
                   help="Short brief in any language; Context-IR will rewrite it.")
    p.add_argument("--ref-image", action="append", default=None,
                   help="Local path or https URL of a bag reference image. Repeatable.")
    p.add_argument("--ref-video", action="append", default=None,
                   help="Local path or URL of a scene-anchor video. Repeatable but only first is used.")
    p.add_argument("--duration", type=int, default=10)
    p.add_argument("--ratio", default="9:16")
    p.add_argument("--resolution", default="768P", choices=["768P", "2K"])
    args = p.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
