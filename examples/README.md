# Example runs

Real questions answered by the running service, against the live registry, with the
actual JSON each one produced. Regenerate with `uv run python -m examples.run_examples`.
Because the registry updates daily, a rerun gives slightly different counts.

Claude's reading of a question can also differ between runs where the question allows
two readings. The network question has been read both as all myeloma trials started in
2024 and as only those with a drug intervention (117 trials instead of 186). Either way,
`meta.plan` and the subtitle state exactly which reading was used.

| Example | Recommended chart, and why | Alternatives | Trials |
| --- | --- | --- | --- |
| [Trials of Pembrolizumab started per year](01_assignment_example.json) | `time_series`: change over time reads best as a line | bar_chart, table | 2,659 |
| [Keytruda vs Opdivo: trials by phase](02_compare_keytruda_opdivo.json) | `grouped_bar_chart`: a few bars per group, compared side by side | stacked_bar_chart, normalized_stacked_bar_chart, heatmap, table | 2,053 / 1,265 |
| [Recruiting melanoma trials by country](03_recruiting_melanoma_by_country.json) | `horizontal_bar_chart`: a ranking reads best as bars sorted by length, with room for long names | choropleth_map, treemap, table | 471 |
| [Network of lead sponsors and interventions in multiple myeloma trials](04_myeloma_sponsor_drug_network.json) | `network_graph`: requested by the question | network_graph_3d, table | 186 |
| [Lung cancer trials: sponsor class to phase to overall status](05_lung_cancer_flow.json) | `sankey`: how trials move from one stage to the next | table | 516 |

Each file shows the full response: the visualization with its rows and citations, the
alternative views, and `meta` with the resolved plan, the source of every plan value,
every note, how the chart was chosen (including the charts ruled out and why), the trials
that only mention the drug or condition, and every registry request made.
