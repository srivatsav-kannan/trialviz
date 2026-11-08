# Visualization

How the chart is chosen, and what a frontend receives. Every answer has one recommended chart, the reason for it, and every other chart that can draw the same data, each with its reason. Charts that cannot draw the data are listed too, with why.

## The catalog

The shape of the data decides which charts can draw it:

| Data shape | Charts |
| --- | --- |
| One number | `stat` |
| Counts per category | `bar_chart`, `horizontal_bar_chart`, `donut_chart`, `treemap`, `choropleth_map`, `table` |
| Counts over two categories | `grouped_bar_chart`, `stacked_bar_chart`, `normalized_stacked_bar_chart`, `heatmap`, `table` |
| Counts over time | `time_series`, `stacked_area_chart`, `bar_chart`, `grouped_bar_chart`, `heatmap`, `table` |
| Numeric values | `histogram`, `box_plot`, `table` |
| One point per trial | `scatter_plot`, `bubble_chart`, `scatter_plot_3d`, `table` |
| Nodes and links | `network_graph`, `network_graph_3d`, `table` |
| Flows across stages | `sankey`, `table` |
| Individual trials | `timeline`, `table` |

## How the chart is chosen

The choice is made by rules, never by a model, so the same data always gets the same chart (`visualization/recommend.py`):

1. **Shape.** The dataset's shape selects the candidate charts.
2. **Fit.** Each candidate checks facts about the actual data, and either fits or says why not:
   - A donut needs 2 to 5 groups that are shares of one whole. Countries, drugs and conditions never qualify, since a trial can be in several.
   - A line needs at least 3 periods and at most 8 series.
   - Grouped bars need 5 or fewer bars per group. The smaller dimension becomes the bars within each group, so Keytruda vs Opdivo by phase shows phases on the axis with two bars each.
   - Stacked and normalized bars need each trial in exactly one segment.
   - A choropleth needs countries.
   - A box plot needs at least 2 groups with 5 or more values.
   - A scatter needs at least 5 trials with every measure.
   - A timeline shows at most 60 trials.
3. **Score.** Fitting candidates are scored on documented preferences, and the highest wins:
   - Lines for change over time.
   - Sorted horizontal bars for rankings and long names, because lengths compare more precisely than colors or areas.
   - Natural order for phases and years.
   - A world map when there are many countries and the question is not a ranking.
   - Shares (normalized bars) when compared groups differ in size by more than 2 to 1.
   - A heatmap when there are too many combinations for bars.
   - A 3D network when a network is too dense to read flat.
4. **Requests.** A chart named by the caller (`presentation.chart_type`), or by the question ("show it as a map"), wins whenever it fits. When it does not, the recommendation stands and a note says why: "A donut chart was requested, but a donut needs groups that are shares of one whole, but a trial can be in several of these groups, so a horizontal bar chart is shown instead."

`meta.chart_choice` reports the outcome: the recommended chart and reason, the alternatives with theirs, and the charts ruled out with the rule each failed.

Example: "Which countries have the most recruiting melanoma trials?" gives counts over 53 countries. The donut is ruled out (a trial can be in several countries), and the bar chart is ruled out (more than 30 bars). The question asks for a ranking, so the horizontal bar chart scores highest. The alternatives are a choropleth map, a treemap and a table.

## What a frontend receives

An abridged excerpt of [the assignment example's actual response](../examples/01_assignment_example.json):

```json
{
  "visualization": {
    "type": "time_series",
    "title": "Trials of Pembrolizumab started per year",
    "subtitle": null,
    "reason": "change over time reads best as a line",
    "encoding": {
      "x": {"field": "period", "type": "temporal", "title": "Start year"},
      "y": {"field": "trial_count", "type": "quantitative", "title": "Trials"}
    },
    "data": [
      {"period": "2015", "trial_count": 116, "nct_ids": ["NCT02586207", "..."], "citations": [{"nct_id": "NCT02586207", "field": "protocolSection.statusModule.startDateStruct.date", "excerpt": "2015-11", "kind": "value", "url": "https://clinicaltrials.gov/study/NCT02586207"}]}
    ],
    "options": {},
    "vega_lite": {"$schema": "https://vega.github.io/schema/vega-lite/v5.json", "mark": {"type": "line", "point": true}, "...": "..."}
  },
  "alternatives": [
    {"type": "bar_chart", "reason": "one bar per period", "encoding": {"...": "..."}, "data": null, "vega_lite": {"...": "..."}}
  ]
}
```

- **`encoding`** maps visual channels to row fields, with a type (nominal, ordinal, quantitative, temporal), a title, and where it matters a sort order, a stack mode, or `"scale": "symlog"` for a heavily skewed measure. A symmetric log scale behaves like a log scale for large values but stays linear near zero, so trials that report zero enrollment are still drawn. The Vega-Lite spec labels such an axis 0, 1, 2, 5, 10, 20, 50 and so on, since Vega's default ticks on it are linear and leave the small values unlabeled.
- **`data`** holds the rows. Alternatives that draw the same rows set `data` to null and reuse the recommended chart's rows, so switching views needs no request. An alternative with a different row shape (bins next to box statistics) carries its own.
- **`options`** carry chart-specific details: `display_limit` when a bar chart has too many bars to show at once (the data stays complete), the TopoJSON source for maps, the network relation, and the Sankey stages.
- **`vega_lite`** is a complete Vega-Lite spec, generated mechanically from the encoding, for every chart Vega-Lite can draw. Citations and trial ID lists are left out of the embedded data to keep it small, and stay in `data`. Treemaps, 3D charts, networks and Sankey diagrams have no Vega-Lite form and get null.

Row formats by chart family are listed in [api.md](api.md#rows-by-chart).

## 3D

Networks and scatter plots have 3D variants:

- **`network_graph_3d`** shares the 2D network's nodes and edges. Every node carries a precomputed 2D position (`x`, `y`) and 3D position (`x3`, `y3`, `z3`) from a deterministic force-directed layout, scaled to -1..1. A frontend with its own layout engine can ignore them, and one without (such as a plain three.js scene) can draw the graph directly. The 3D view is recommended over the flat one when the network has more than 80 nodes.
- **`scatter_plot_3d`** is offered when the question has a third measure (`analysis.z`). The flat alternative is a bubble chart that shows the third measure as point size.

## The demo UI

The service serves a single page at `/` (`src/trialviz/ui/`) that calls `/v1/query` and draws every chart type. It is a reference consumer of the response, so it decides nothing about the data:

- Every view in `visualization` and `alternatives` becomes a tab, with the backend's reason under it. Switching tabs makes no request.
- Charts with a `vega_lite` spec are drawn by vega-embed from that spec, with only the theme and size changed. Treemaps, Sankey diagrams and 3D scatter plots use Plotly, and networks use force-graph and 3d-force-graph at the positions the backend computed. The 3D libraries load only when a 3D view is opened.
- Selecting a bar, point, cell, node, edge or table row shows that datum's trials and citations: the registry field and the exact value each trial was counted by, linked to the record.
- "How the question was read" lists the resolved plan with the source of each value, and the notes, mention-only trials and registry requests follow.
- The optional fields map to the request's shorthands, so they are binding in the same way. Clarifications and errors are shown as the API returns them.

It is plain HTML, CSS and one JavaScript module with no build step, follows the system's light or dark mode, and works at phone width.

## Titles

Titles are written from the plan, deterministically: what was counted and how it was grouped, such as "Recruiting melanoma trials by country" or "Keytruda vs Opdivo: trials by phase". Filters that do not fit the title (countries, years, sponsor and study types) go in the subtitle, so no applied constraint goes unstated.
