"""Draw the article's figures. Chart values are read from eval/results, never typed in."""
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

HERE = Path(__file__).parent
RESULTS = HERE.parent / "eval" / "results"
OUT = HERE / "figures"
OUT.mkdir(exist_ok=True)

INK, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#ffffff"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "axes.edgecolor": MUTED,
                     "axes.labelcolor": MUTED, "xtick.color": INK, "ytick.color": MUTED, "text.color": INK})


def clean(ax):
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(length=0)


def bars(ax, groups, series, colors, fmt):
    """Grouped bars with a 2 px style surface gap and a value on every bar (print has no hover)."""
    width = 0.8 / len(series)
    for i, (label, values) in enumerate(series.items()):
        xs = [g + (i - (len(series) - 1) / 2) * width for g in range(len(groups))]
        drawn = ax.bar(xs, values, width * 0.92, color=colors[i], label=label, edgecolor=SURFACE, linewidth=1)
        for bar, value in zip(drawn, values):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), fmt(value), ha="center", va="bottom",
                    fontsize=8, color=INK)
    ax.set_xticks(range(len(groups)), groups)


def accuracy():
    load = lambda name: json.loads((RESULTS / f"planner_{name}_test.json").read_text())["summary"]
    rules, plain, ctx = load("rules_none"), load("slm-nocontext_phi3-gpu"), load("slm-context_phi3-gpu")
    groups = [f"Context-dependent\nrequests (n={ctx['contextual']['n']})",
              f"Self-contained\nrequests (n={ctx['explicit']['n']})", f"All requests\n(n={ctx['all']['n']})"]
    pick = lambda s: [s[k]["exact_accuracy"] * 100 for k in ("contextual", "explicit", "all")]
    fig, ax = plt.subplots(figsize=(6.3, 2.35), dpi=220)
    bars(ax, groups, {"Keyword rules": pick(rules), "Phi-3, no context": pick(plain), "Phi-3 with context": pick(ctx)},
         [BLUE, ORANGE, AQUA], lambda v: f"{v:.1f}")
    ax.set_ylabel("Exact plan accuracy (%)")
    ax.set_ylim(0, 112)
    ax.set_yticks([0, 25, 50, 75, 100])
    clean(ax)
    ax.legend(frameon=False, ncol=3, loc="upper center", bbox_to_anchor=(0.5, 1.2), fontsize=8.5)
    fig.tight_layout()
    fig.savefig(OUT / "accuracy.png")


def governor():
    data = json.loads((RESULTS / "governor_bench.json").read_text())["summary"]
    groups = ["Mean", "p95"]
    pick = lambda c: [data[c]["mean_ms"], data[c]["p95_ms"]]
    fig, ax = plt.subplots(figsize=(6.3, 2.15), dpi=220)
    bars(ax, groups, {"No load": pick("idle"), "Load, governor off": pick("contended"), "Load, governor on": pick("governed")},
         [BLUE, ORANGE, AQUA], lambda v: f"{v:.1f}")
    ax.set_ylabel("Foreground task\nresponse time (ms)")
    ax.set_ylim(0, 175)
    clean(ax)
    ax.legend(frameon=False, ncol=3, loc="upper center", bbox_to_anchor=(0.5, 1.22), fontsize=8.5)
    fig.tight_layout()
    fig.savefig(OUT / "governor.png")


def box(ax, x, y, w, h, title, sub="", fill="#eef4fc", edge=BLUE):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.08", fc=fill, ec=edge, lw=1))
    if sub:
        ax.text(x + w / 2, y + h * 0.64, title, ha="center", va="center", fontsize=8, fontweight="bold")
        ax.text(x + w / 2, y + h * 0.28, sub, ha="center", va="center", fontsize=6.6, color=MUTED)
    else:
        ax.text(x + w / 2, y + h / 2, title, ha="center", va="center", fontsize=8, fontweight="bold")


def arrow(ax, a, b, color=MUTED, style="-"):
    ax.add_patch(FancyArrowPatch(a, b, arrowstyle="-|>", mutation_scale=9, lw=1, color=color, linestyle=style,
                                 shrinkA=0, shrinkB=0))


def architecture():
    fig, ax = plt.subplots(figsize=(6.5, 3.15), dpi=220)
    ax.set_xlim(0, 10.2)
    ax.set_ylim(0, 4.95)
    ax.axis("off")
    orange_f, orange_e = "#fdf0ea", ORANGE
    aqua_f, aqua_e = "#e8f7f1", AQUA
    grey_f, grey_e = "#f1f0ec", MUTED

    box(ax, 0.1, 4.25, 10.0, 0.6, "User: natural-language shell (CLI) and local web dashboard", fill=grey_f, edge=grey_e)

    ax.text(10.1, 4.03, "SLOW PATH: about 1 s, only when the user asks", fontsize=6.8, color=ORANGE, fontweight="bold", va="center", ha="right")
    y, h = 3.0, 0.88
    box(ax, 0.1, y, 2.25, h, "Inference service", "Phi-3-mini, priority queue,\nload / evict / admission", orange_f, orange_e)
    box(ax, 2.65, y, 1.75, h, "Planner", "prompt + context,\nJSON tool call", orange_f, orange_e)
    box(ax, 4.7, y, 1.75, h, "Validator", "schema check of\nuntrusted output", orange_f, orange_e)
    box(ax, 6.75, y, 1.65, h, "Policy monitor", "allow, confirm\nor deny", orange_f, orange_e)
    box(ax, 8.7, y, 1.4, h, "Audit log", "SHA-256\nhash chain", orange_f, orange_e)
    arrow(ax, (2.65, y + h * .65), (2.35, y + h * .65))
    arrow(ax, (2.35, y + h * .3), (2.65, y + h * .3))
    arrow(ax, (4.4, y + h / 2), (4.7, y + h / 2))
    arrow(ax, (6.45, y + h / 2), (6.75, y + h / 2))
    arrow(ax, (8.4, y + h / 2), (8.7, y + h / 2))
    arrow(ax, (3.525, 4.25), (3.525, y + h))

    ax.text(3.7, 2.76, "FAST PATH: always running, no model", fontsize=6.8, color="#0f7d56", fontweight="bold", va="center")
    y2 = 1.72
    box(ax, 0.1, y2, 2.25, h, "Sensors", "CPU, memory, disk, battery,\nforeground app, idle time", aqua_f, aqua_e)
    box(ax, 2.65, y2, 1.75, h, "Context engine", "activity rules, SQLite\nstore, prompt summary", aqua_f, aqua_e)
    box(ax, 4.7, y2, 1.75, h, "Governor", "feedback loop with\nhysteresis", aqua_f, aqua_e)
    box(ax, 6.75, y2, 3.35, h, "Toolbox (12 tools)", "status, processes, files, priority, focus,\nlaunch, terminate (needs confirmation)", aqua_f, aqua_e)
    arrow(ax, (2.35, y2 + h / 2), (2.65, y2 + h / 2))
    arrow(ax, (4.4, y2 + h / 2), (4.7, y2 + h / 2))
    arrow(ax, (3.525, y2 + h), (3.525, y))
    arrow(ax, (7.575, y), (7.575, y2 + h))

    y3 = 0.1
    box(ax, 0.1, y3, 10.0, 0.95, "Unmodified operating system kernel (Windows 11 or Linux)",
        "scheduler priority classes / nice values, process table, virtual memory, file system", grey_f, grey_e)
    arrow(ax, (1.225, y3 + .95), (1.225, y2))
    arrow(ax, (5.575, y2), (5.575, y3 + .95))
    arrow(ax, (8.4, y2), (8.4, y3 + .95))
    ax.text(1.32, 1.38, "read state", fontsize=6.6, color=MUTED, va="center")
    ax.text(5.67, 1.38, "set priorities", fontsize=6.6, color=MUTED, va="center")
    ax.text(8.5, 1.38, "actions", fontsize=6.6, color=MUTED, va="center")
    fig.subplots_adjust(0, 0, 1, 1)
    fig.savefig(OUT / "architecture.png")


if __name__ == "__main__":
    accuracy()
    governor()
    architecture()
