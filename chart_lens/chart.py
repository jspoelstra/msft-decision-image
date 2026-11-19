"""Deterministic, anonymized chart shared by inspection and inference."""

from io import BytesIO
from threading import Lock

from matplotlib import rc_context
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.ticker import PercentFormatter, StrMethodFormatter

from chart_lens.domain import ChartInput

TEMPLATE: str = "two-panel-pair-v3"
_RENDER_LOCK = Lock()


def render(chart: ChartInput) -> bytes:
    """Render a 1568 by 980 PNG within Decision's per-side pixel limit."""
    with _RENDER_LOCK, rc_context({"font.family": "DejaVu Sans", "font.size": 12}):
        figure = Figure(figsize=(16, 10), dpi=98, facecolor="white", layout="constrained")
        canvas = FigureCanvasAgg(figure)
        price, relative = figure.subplots(2, 1, sharex=True)
        x = tuple(range(1, 31))
        ink = "#162844"
        blue = "#195dc5"
        figure.suptitle("30-session price context", fontsize=23, fontweight="bold", color=ink)

        price.plot(x, chart.close, color=blue, linewidth=2.7, label="Target close")
        price.scatter(x[-1], chart.close[-1], color=blue, s=56, zorder=4)
        price.axhline(
            chart.high, color="#08785c", linestyle="--", label=f"52-week high  ${chart.high:,.2f}"
        )
        price.axhline(
            chart.low, color="#a13d42", linestyle="--", label=f"52-week low  ${chart.low:,.2f}"
        )
        padding = max((chart.high - chart.low) * 0.08, chart.high * 0.002)
        price.set_ylim(chart.low - padding, chart.high + padding)
        price.yaxis.set_major_formatter(StrMethodFormatter("${x:,.2f}"))
        price.set_title("Target closing price and trailing range", loc="left", color=ink)
        price.set_ylabel("Price (USD)")
        price.legend(loc="upper left", ncol=3, frameon=False, bbox_to_anchor=(0, 1.01))

        for (name, values), color, line in zip(
            chart.relative.items(),
            (blue, "#a84d14"),
            ("-", "--"),
            strict=True,
        ):
            relative.plot(
                x,
                values,
                label=name,
                color=color,
                linestyle=line,
                linewidth=2.4 if name == "Target" else 1.9,
            )
        relative.axhline(0, color="#64748b", linewidth=1)
        relative.yaxis.set_major_formatter(PercentFormatter(xmax=100))
        relative.set_title("Target and Comparison price paths", loc="left", color=ink)
        relative.set_ylabel("Change since session 1")
        relative.set_xlabel("Trading session (oldest to latest)")
        relative.set_xticks([1, 5, 10, 15, 20, 25, 30])
        relative.legend(loc="upper left", ncol=2, frameon=False, bbox_to_anchor=(0, 1.01))
        for panel in (price, relative):
            panel.grid(axis="y", alpha=0.22)
            panel.spines[["top", "right"]].set_visible(False)
            panel.set_xlim(0.5, 30.5)
        with BytesIO() as output:
            canvas.print_png(output, metadata={"Software": None})
            return output.getvalue()
