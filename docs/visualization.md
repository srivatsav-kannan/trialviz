# Visualization

This document covers how the chart for an answer is chosen and what a frontend receives in order to draw it. Every answer comes with one recommended chart and the reason for it, every other chart that can draw the same data with its own reason, and the charts that were ruled out along with the rule each one failed.

## The chart catalog

The shape of the aggregated data ([aggregation.md](aggregation.md#dataset-shapes)) decides which of the 22 chart types can draw it.

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

The choice is made by fixed rules in `visualization/recommend.py`, so the same data always gets the same chart and the language model plays no part in it.

The dataset's shape first selects the candidate charts from the catalog above. Each candidate then checks facts about the actual data and either fits or states why it does not. A donut needs 2 to 5 groups that are shares of one whole, which rules out countries, drugs and conditions, since a trial can belong to several of those. A line needs at least 3 periods and at most 8 series. Grouped bars need 5 or fewer bars per group, and the smaller dimension becomes the bars within each group, so Keytruda vs Opdivo by phase puts the phases on the axis with two bars at each. Stacked and normalized bars need every trial to sit in exactly one segment, a choropleth needs countries, a box plot needs at least 2 groups with 5 or more values each, a scatter plot needs at least 5 trials that have every measure, and a timeline shows at most 60 trials.

Candidates that fit are scored on a short list of documented preferences, and the highest score wins. Lines are preferred for change over time. Sorted horizontal bars are preferred for rankings and long names, because lengths compare more precisely than colors or areas. Phases and years keep their natural order, a world map is preferred when there are many countries and the question is not a ranking, normalized bars are preferred when the compared groups differ in size by more than 2 to 1, a heatmap takes over when there are too many combinations for bars, and a 3D network is preferred when a network has more than 80 nodes.

A chart that the caller names in `presentation.chart_type`, or that the question asks for ("show it as a map"), wins whenever it fits. When it does not fit, the recommendation stands and a note explains which rule the requested chart failed.

`meta.chart_choice` reports the outcome in full. For "Which countries have the most recruiting melanoma trials?", the data holds counts for 53 countries. The donut is ruled out because a trial can be in several countries, and the plain bar chart is ruled out because more than 30 bars cannot be read side by side. The question asks for a ranking, so the horizontal bar chart scores highest, and the alternatives are a choropleth map, a treemap and a table.

## What a frontend receives

This is an abridged excerpt of [the assignment example's actual response](../examples/01_assignment_example.json):

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

`encoding` maps each visual channel to a row field, with a type (nominal, ordinal, quantitative or temporal), a title, and where it matters a sort order, a stack mode, or `"scale": "symlog"` for a heavily skewed measure. A symmetric log scale behaves like a log scale for large values and stays linear near zero, so trials that report zero enrollment are still drawn. The generated Vega-Lite spec labels such an axis 0, 1, 2, 5, 10, 20, 50 and so on, because Vega's default ticks on a symlog axis are linear and leave the small values unlabeled.

`data` holds the rows. An alternative that draws the same rows sets its own `data` to null and reuses the recommended chart's rows, so a frontend can switch views without another request, while an alternative with a different row shape (histogram bins next to box statistics, for example) carries its own rows. The row format for each chart family is listed in [api.md](api.md#rows-by-chart).

`options` carries chart-specific details: `display_limit` when a bar chart has more bars than fit at once (the data itself stays complete), the TopoJSON source for maps, the relation a network draws, and the stages of a Sankey diagram.

`vega_lite` is a complete Vega-Lite spec, generated mechanically from the encoding, for every chart that Vega-Lite can draw. Citations and trial ID lists are left out of the embedded data to keep it small, and they remain in `data`. Treemaps, 3D charts, networks and Sankey diagrams have no Vega-Lite form, so their `vega_lite` is null.

## 3D charts

`network_graph_3d` shares the nodes and edges of the flat network. Every node carries a precomputed 2D position (`x`, `y`) and 3D position (`x3`, `y3`, `z3`) from a deterministic force-directed layout scaled to the range -1 to 1, which means a frontend with its own layout engine can ignore them and one without (a plain three.js scene, say) can draw the graph directly. The 3D view is recommended over the flat one when the network has more than 80 nodes.

`scatter_plot_3d` is offered when the question names a third measure (`analysis.z`), and its flat alternative is a bubble chart that shows the third measure as point size.

## Titles

Titles are written deterministically from the plan, stating what was counted and how it was grouped, as in "Recruiting melanoma trials by country" or "Keytruda vs Opdivo: trials by phase". Filters that do not fit naturally in the title (countries, years, sponsor and study types) go in the subtitle, so every applied constraint is stated somewhere on the chart.

## The demo UI

The service also serves a single page at `/`, from `src/trialviz/ui/`, which shows what a frontend can build from the response. It renders saved sample answers and live questions, draws whichever view is selected using the response's own specs and rows, and shows the citations behind any datum that is selected. It makes no decisions about the data, since the chart choice, the reasons and the numbers all come from the backend. The page is plain HTML, CSS and JavaScript with no build step.
