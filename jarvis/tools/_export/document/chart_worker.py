"""Private subprocess entry point for validated chart specs, never a tool API.

All Matplotlib global state stays in this short-lived process. No caller kwargs,
code, images or URLs reach Matplotlib; labels are literal text, including '$'.
"""

import base64
import io
import json
import math
import sys
import textwrap
import unicodedata
import warnings

_COLORS = ("#1f4e79", "#2e7d6b", "#a9791c", "#6b4a7a", "#b45f42", "#427c93", "#746b36", "#965977")


def build_figure(spec, colors=_COLORS):
	from matplotlib.figure import Figure
	from matplotlib.ticker import MaxNLocator, ScalarFormatter

	kind, labels, series = spec["type"], spec["labels"], spec["series"]
	wrapped_labels = [textwrap.fill(label, 26 if kind == "bar" else 20) for label in labels]
	if kind in {"column", "line"} and len(labels) > 6:
		# After a 90-degree rotation, wrapped lines consume horizontal space.
		# Word boundaries can create six lines from a legal80-character label;
		# widen its wrap until at most four lines share each categorical slot.
		for wrap_width in range(20, 41, 2):
			wrapped_labels = [textwrap.fill(label, wrap_width) for label in labels]
			if max(label.count("\n") + 1 for label in wrapped_labels) <= 4:
				break
	label_lines = max(label.count("\n") + 1 for label in wrapped_labels)
	if kind == "bar":
		height = min(8, max(4.5, len(labels) * (label_lines * 10 + 3) / 72 + 1.4))
	else:
		height = 7 if label_lines > 2 else 4.8
	fig = Figure(figsize=(8, height), dpi=150, facecolor="white", layout="constrained")
	ax = fig.add_subplot(111)
	if kind == "pie":
		values = series[0]["values"]
		legend = [
			textwrap.fill(f"{label} ({value:g}{' ' + spec['unit'] if spec['unit'] else ''})", 26)
			for label, value in zip(labels, values, strict=True)
		]
		legend_lines = sum(label.count("\n") + 1 for label in legend)
		fig.set_size_inches(8, min(8, max(4.8, (legend_lines * 10 + len(labels) * 5 + 24) / 72)))
		# pie() casts to float32: scaling first preserves even a positive
		# float64 subnormal total, instead of silently producing NaN wedges.
		scaled = [value / max(values) for value in values]
		wedges, _, percentages = ax.pie(
			scaled,
			colors=colors[: len(values)],
			startangle=90,
			autopct=lambda pct: f"{pct:.1f}%" if pct >= 4 else "",
			pctdistance=0.7,
			textprops={"fontsize": 9},
			wedgeprops={"edgecolor": "white", "linewidth": 1},
		)
		for wedge, text in zip(wedges, percentages, strict=True):
			channels = [
				channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
				for channel in wedge.get_facecolor()[:3]
			]
			luminance = sum(
				channel * weight for channel, weight in zip(channels, (0.2126, 0.7152, 0.0722), strict=True)
			)
			text.set_color("white" if luminance < 0.179 else "black")
		fig.legend(wedges, legend, loc="outside right center", frameon=False, fontsize=8)
		return fig

	positions = list(range(len(labels)))
	width = 0.8 / len(series)
	handles = []
	for index, item in enumerate(series):
		values = [math.nan if value is None else value for value in item["values"]]
		name = item["name"] or None
		if kind == "line":
			(handle,) = ax.plot(
				positions, values, marker="o", markersize=3, linewidth=1.7, color=colors[index], label=name
			)
		else:
			offsets = [position - 0.4 + width / 2 + index * width for position in positions]
			if kind == "bar":
				handle = ax.barh(offsets, values, height=width, color=colors[index], label=name)
			else:
				handle = ax.bar(offsets, values, width=width, color=colors[index], label=name)
		handles.append(handle)
	if kind == "bar":
		ax.set_yticks(positions, wrapped_labels)
		# Half a category of breathing room keeps the first/last group intact
		# without autoscale's extra vertical margin crowding dense wrapped labels.
		ax.set_ylim(len(labels) - 0.5, -0.5)
		ax.axvline(0, color="#667085", linewidth=0.7)
		ax.set_xlabel(textwrap.fill(spec["unit"], 30), fontsize=9)
		axis, grid_axis = ax.xaxis, "x"
	else:
		# Show at most twelve labels without throwing away any observations.
		ticks = sorted(
			{
				round(i * (len(labels) - 1) / min(11, max(1, len(labels) - 1)))
				for i in range(min(12, len(labels)))
			}
		)
		ax.set_xticks(
			ticks,
			[wrapped_labels[i] for i in ticks],
			rotation=90 if len(labels) > 6 and label_lines > 1 else (35 if len(labels) > 6 else 0),
			ha="center" if label_lines > 1 or len(labels) <= 6 else "right",
		)
		ax.axhline(0, color="#667085", linewidth=0.7)
		ax.set_ylabel(textwrap.fill(spec["unit"], 30), fontsize=9)
		axis, grid_axis = ax.yaxis, "y"
	axis.set_major_locator(MaxNLocator(nbins=6))
	formatter = ScalarFormatter(useOffset=False)
	formatter.set_powerlimits((-3, 6))
	axis.set_major_formatter(formatter)
	ax.tick_params(axis="both", labelsize=8, length=0, pad=6)
	ax.set_axisbelow(True)
	ax.grid(axis=grid_axis, color="#e4e7ec", linewidth=0.6)
	for spine in ax.spines.values():
		spine.set_visible(False)
	if len(series) > 1 or series[0]["name"]:
		# Explicit handles preserve legitimate names beginning with '_', which
		# Matplotlib's automatic legend collection deliberately excludes.
		fig.legend(
			handles,
			[textwrap.fill(item["name"], 22) for item in series],
			loc="outside upper center",
			ncol=min(2, len(series)),
			frameon=False,
			fontsize=8,
		)
	return fig


def main():
	import matplotlib
	from matplotlib.backends.backend_agg import FigureCanvasAgg

	matplotlib.rcParams.update({"text.usetex": False, "text.parse_math": False, "font.family": "DejaVu Sans"})
	batch = json.load(sys.stdin)
	images = {}
	omitted = {}
	for index, spec in batch["charts"].items():
		texts = [*spec["labels"], spec["unit"], *(item["name"] for item in spec["series"])]
		# Agg has no bidi/shaping engine; even present glyphs would be drawn in
		# their logical order, producing misleading labels.
		if any(unicodedata.bidirectional(char) in {"R", "AL", "AN"} for text in texts for char in text):
			omitted[index] = "unsupported_text"
			continue
		fig = None
		try:
			with warnings.catch_warnings():
				# Stop at the first unsupported glyph instead of painting boxes or
				# accumulating warnings containing caller text.
				warnings.simplefilter("ignore")
				warnings.filterwarnings(
					"error",
					message=r"Glyph .*missing from font|Matplotlib currently does not support .* natively\.",
					category=UserWarning,
				)
				fig = build_figure(spec, tuple(batch["colors"]) + _COLORS[4:])
				buffer = io.BytesIO()
				FigureCanvasAgg(fig).print_png(buffer)
				images[index] = base64.b64encode(buffer.getvalue()).decode("ascii")
		except UserWarning:
			omitted[index] = "unsupported_text"
		finally:
			if fig is not None:
				fig.clear()
	json.dump({"images": images, "omitted": omitted}, sys.stdout)


if __name__ == "__main__":
	main()
