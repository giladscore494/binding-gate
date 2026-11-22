"""Static figures rendered from ``analysis.json`` (matplotlib, imported lazily).

Figures
-------
``primary_pair.png``
    Small multiples, one row per model, two panels: M1 (wrong among answered)
    and M2 (correct answers blocked), each arm a point with its cluster-bootstrap
    interval. The pair is always shown together (PREREGISTRATION.md section 7).
``conditions_<model>.png``
    Per model, one heatmap per arm: wrong-entity answers / all records for the
    15 conditions (rows: competing-document condition; columns: X-document
    completeness). Exploratory.

Design: one series per panel (arms are labelled on the axis, so no colour
legend is needed), thin interval lines, recessive grid, a single-hue sequential
ramp for the heatmap with every cell labelled with its value.
"""

from __future__ import annotations

import re
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping

from binding_gate.stats.metrics import arm_key

SERIES = "#2a78d6"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
SURFACE = "#fcfcfb"
SEQUENTIAL = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
X_ORDER = ("absent", "partial", "complete")
Y_ORDER = ("null_control", "class_proximate", "context_adjacent", "prior_completing", "synthetic_y")


def available() -> bool:
    try:
        import matplotlib  # noqa: F401
    except ImportError:
        return False
    return True


def _pyplot():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.size": 9,
            "axes.edgecolor": MUTED,
            "axes.labelcolor": INK_SECONDARY,
            "xtick.color": MUTED,
            "ytick.color": INK_SECONDARY,
            "axes.titlecolor": INK,
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "svg.hashsalt": "binding-gate",
        }
    )
    return plt


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def _style(ax) -> None:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def _panel(ax, model: Mapping[str, Any], arms: list[str], stat: str, title: str) -> None:
    from matplotlib.ticker import PercentFormatter

    for y, arm in enumerate(arms):
        entry = model["estimates"].get(arm_key(arm, stat))
        if entry is None or entry["value"] is None:
            ax.text(0, y, "  n/a", va="center", color=MUTED)
            continue
        ci = entry["ci"]
        if ci.get("low") is not None:
            ax.plot([ci["low"], ci["high"]], [y, y], color=SERIES, linewidth=2, solid_capstyle="round")
        ax.plot([entry["value"]], [y], "o", color=SERIES, markersize=6, markeredgecolor=SURFACE, markeredgewidth=1.5)
        right = ci["high"] if ci.get("high") is not None else entry["value"]
        ax.annotate(f"{100 * entry['value']:.1f}%", (right, y), xytext=(6, 0),
                    textcoords="offset points", ha="left", va="center", fontsize=7, color=INK_SECONDARY)
    ax.set_yticks(range(len(arms)), arms)
    ax.set_ylim(len(arms) - 0.5, -0.6)
    ax.set_xlim(left=0, right=max(ax.get_xlim()[1] * 1.12, 0.05))
    ax.xaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.set_title(title, loc="left", fontsize=9)
    _style(ax)


def primary_pair_figure(analysis: Mapping[str, Any], path: Path) -> Path:
    plt = _pyplot()
    models = list(analysis["models"].items())
    n_arms = max(len(m["arms"]) for _n, m in models)
    fig, axes = plt.subplots(len(models), 2, figsize=(9, 0.5 + (0.45 * n_arms + 0.6) * len(models)), squeeze=False)
    for row, (name, model) in enumerate(models):
        arms = list(model["arms"])
        _panel(axes[row][0], model, arms, "m1", f"{name}: M1 wrong among answered")
        others = [a for a in arms if a != model["reference"]]
        if others and model["reference"]:
            _panel(axes[row][1], model, others, "m2", f"{name}: M2 correct answers blocked (vs {model['reference']})")
        else:
            axes[row][1].set_axis_off()
    level = float(Fraction(analysis["parameters"]["used"]["ci_level"]))
    fig.suptitle(
        f"{analysis['dataset']} ({analysis['split']} split): point estimates with {level:.0%} cluster-bootstrap intervals",
        x=0.01, ha="left", fontsize=10, color=INK,
    )
    fig.tight_layout()
    fig.savefig(path, dpi=150, metadata={"Software": None})
    plt.close(fig)
    return path


def condition_heatmap(analysis: Mapping[str, Any], model_name: str, path: Path) -> Path | None:
    from matplotlib.colors import LinearSegmentedColormap

    model = analysis["models"][model_name]
    conditions = model["conditions"]
    cells = {tuple(k.split("/")): v for k, v in conditions.items() if len(k.split("/")) == 2}
    if not cells:
        return None
    xs = [x for x in X_ORDER if any(k[0] == x for k in cells)] + sorted({k[0] for k in cells} - set(X_ORDER))
    ys = [y for y in Y_ORDER if any(k[1] == y for k in cells)] + sorted({k[1] for k in cells} - set(Y_ORDER))
    arms = list(model["arms"])
    plt = _pyplot()
    cmap = LinearSegmentedColormap.from_list("bg_seq", SEQUENTIAL)
    values = [cells[(x, y)][a]["wrong_all"] for a in arms for x in xs for y in ys
              if (x, y) in cells and cells[(x, y)][a]["wrong_all"] is not None]
    vmax = max(values + [0.01])
    fig, axes = plt.subplots(1, len(arms), figsize=(1.0 + 2.4 * len(arms), 0.9 + 0.42 * len(ys)), squeeze=False)
    image = None
    for col, arm in enumerate(arms):
        ax = axes[0][col]
        grid = [[(cells.get((x, y)) or {}).get(arm, {}).get("wrong_all") for x in xs] for y in ys]
        numeric = [[float("nan") if v is None else v for v in row] for row in grid]
        image = ax.imshow(numeric, cmap=cmap, vmin=0, vmax=vmax, aspect="auto")
        for i, row in enumerate(grid):
            for j, v in enumerate(row):
                label = "n/a" if v is None else f"{100 * v:.0f}%"
                dark = v is not None and v > 0.55 * vmax
                ax.text(j, i, label, ha="center", va="center", fontsize=7, color="#ffffff" if dark else INK)
        ax.set_xticks(range(len(xs)), xs, rotation=0)
        ax.set_xlabel("X document")
        if col == 0:
            ax.set_ylabel("competing document")
        ax.set_yticks(range(len(ys)), ys if col == 0 else [""] * len(ys))
        ax.set_title(arm, loc="left", fontsize=9)
        ax.tick_params(length=0)
        for spine in ax.spines.values():
            spine.set_visible(False)
    if image is not None:
        bar = fig.colorbar(image, ax=axes[0].tolist(), fraction=0.03, pad=0.02)
        from matplotlib.ticker import PercentFormatter

        bar.ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
        bar.set_label("wrong-entity / all records", color=INK_SECONDARY)
        bar.outline.set_visible(False)
    fig.suptitle(f"{model_name}: wrong-entity rate by condition (exploratory)", x=0.01, y=1.01,
                 ha="left", va="bottom", fontsize=10, color=INK)
    fig.savefig(path, dpi=150, bbox_inches="tight", metadata={"Software": None})
    plt.close(fig)
    return path


def write_figures(analysis: Mapping[str, Any], out_dir: str | Path) -> list[Path]:
    """Write every figure into ``out_dir``; returns the paths written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [primary_pair_figure(analysis, out_dir / "primary_pair.png")]
    for name in analysis["models"]:
        heatmap = condition_heatmap(analysis, name, out_dir / f"conditions_{_safe(name)}.png")
        if heatmap is not None:
            paths.append(heatmap)
    return paths
