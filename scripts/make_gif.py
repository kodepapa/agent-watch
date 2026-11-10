"""Animated capture of the live demo: python scripts/make_gif.py out.gif [speed]"""
import asyncio
import io
import os
import sys
import tempfile
import time

from PIL import Image
from rich.console import Console

from agentwatch.app import AgentWatchApp
from agentwatch.shot import render_png
from agentwatch.sim import DemoSession
from agentwatch.sources import LiveCollector

OUT = sys.argv[1] if len(sys.argv) > 1 else "agentwatch-live-demo.gif"
SPEED = float(sys.argv[2]) if len(sys.argv) > 2 else 24
COLS, ROWS = 132, 46

root = tempfile.mkdtemp()
claude, events = os.path.join(root, "claude"), os.path.join(root, "events.jsonl")
sim = DemoSession(claude, events)
app = AgentWatchApp(LiveCollector(events, claude, since=86400), mode="demo",
                    advisor_model="opus 5.5", tick=0.1)
frames = []


async def run():
    async with app.run_test(size=(COLS, ROWS)) as pilot:
        sim.run_live(speed=SPEED)
        end = time.time() + sim.duration / SPEED + 1.5
        while time.time() < end:
            await pilot.pause(0.45)
            console = Console(width=COLS, height=ROWS, file=io.StringIO(), force_terminal=True,
                              color_system="truecolor", record=True, legacy_windows=False)
            console.print(app.screen._compositor.render_update(full=True))
            frames.append(list(console._record_buffer))

asyncio.run(run())
images = []
for i, segments in enumerate(frames):
    path = os.path.join(root, f"frame_{i:03d}.png")
    render_png(segments, COLS, ROWS, path, title="agentwatch demo", scale=1)
    images.append(Image.open(path).convert("RGB"))
palette = images[len(images) // 2].quantize(colors=128, method=Image.Quantize.MEDIANCUT)
frames_q = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in images]
frames_q[0].save(OUT, save_all=True, append_images=frames_q[1:],
                 duration=[180] * (len(frames_q) - 1) + [2500], loop=0, optimize=True)
print(OUT, len(frames_q), "frames")
