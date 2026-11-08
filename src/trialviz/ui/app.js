// trialviz demo page. It shows saved sample answers, asks /v1/query for new ones, renders the
// recommended chart and its alternatives, and shows the registry evidence behind whatever is
// selected. It renders what the response says and decides nothing about the data itself.
//
// All registry text is inserted with textContent, never as HTML, so a trial title cannot
// inject markup. Heavy libraries (Plotly, 3D graphs) load only when a chart needs them.

const LIBRARIES = {
  plotly: "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js",
  graph3d: "https://cdn.jsdelivr.net/npm/3d-force-graph@1.80.1/dist/3d-force-graph.min.js",
};

const CHART_LABELS = {
  stat: "Number",
  bar_chart: "Bar chart",
  horizontal_bar_chart: "Ranked bars",
  donut_chart: "Donut",
  treemap: "Treemap",
  choropleth_map: "World map",
  table: "Table",
  grouped_bar_chart: "Grouped bars",
  stacked_bar_chart: "Stacked bars",
  normalized_stacked_bar_chart: "Shares",
  heatmap: "Heatmap",
  time_series: "Line chart",
  stacked_area_chart: "Stacked area",
  histogram: "Histogram",
  box_plot: "Box plot",
  scatter_plot: "Scatter plot",
  bubble_chart: "Bubble chart",
  scatter_plot_3d: "3D scatter",
  network_graph: "Network",
  network_graph_3d: "3D network",
  sankey: "Flow",
  timeline: "Timeline",
};

// Live questions that reach chart types the saved samples do not.
const SUGGESTIONS = [
  "Enrollment by phase for completed asthma trials",
  "List the recruiting phase 3 trials of tirzepatide",
  "Enrollment versus duration and site count for completed phase 3 psoriasis trials",
  "How many semaglutide trials are recruiting in Germany?",
];

// The icon and tone a sample card wears, by the chart family it opens on.
const SAMPLE_LOOK = {
  time_series: ["i-capsule", 1],
  grouped_bar_chart: ["i-compare", 2],
  horizontal_bar_chart: ["i-globe", 3],
  choropleth_map: ["i-globe", 3],
  network_graph: ["i-network", 5],
  network_graph_3d: ["i-network", 5],
  sankey: ["i-flow", 4],
};

const SOURCE_LABELS = {
  user: "from your fields",
  inferred: "from the question",
  derived: "implied",
  default: "default",
};

const HEAVY = new Set(["citations", "nct_ids", "outliers"]);

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const loaded = {};
function loadScript(name) {
  loaded[name] ??= new Promise((resolve, reject) => {
    const script = el("script", { src: LIBRARIES[name] });
    script.onload = resolve;
    script.onerror = () => reject(new Error(`Could not load ${name}`));
    document.head.append(script);
  });
  return loaded[name];
}

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
// Categorical slots in a fixed order that passes colorblind-separation checks for adjacent
// series, and a single-hue blue ramp for magnitude (heatmaps and the map).
const SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"];
const RAMP = ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"];
const palette = () => SERIES;
const number = (value) => (typeof value === "number" ? value.toLocaleString() : value);

// --- State ---------------------------------------------------------------------------------

let current = null; // { answer, views: [{view, data}], active }

// --- Form ------------------------------------------------------------------------------------

function setUpForm() {
  const select = document.querySelector('select[name="chart_type"]');
  for (const [value, label] of Object.entries(CHART_LABELS)) select.append(el("option", { value }, label));

  const list = $("suggestions");
  for (const query of SUGGESTIONS) {
    list.append(el("li", {}, el("button", { type: "button", onclick: () => runSuggestion(query) }, query)));
  }

  $("ask-form").addEventListener("submit", (event) => {
    event.preventDefault();
    ask(buildRequest());
  });
  $("question").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) $("ask-form").requestSubmit();
  });
}

function runSuggestion(query) {
  $("ask-form").reset();
  $("question").value = query;
  ask(buildRequest());
}

function buildRequest() {
  const form = $("ask-form").elements;
  const request = { query: form.query.value.trim() };
  for (const name of ["drug_name", "condition", "trial_phase", "country"]) {
    if (form[name].value.trim()) request[name] = form[name].value.trim();
  }
  for (const name of ["start_year", "end_year"]) {
    // Anything but a plain year goes through as typed, so the API's validation explains it.
    const raw = form[name].value.trim();
    if (raw) request[name] = /^\d+$/.test(raw) ? Number(raw) : raw;
  }
  if (form.chart_type.value) request.presentation = { chart_type: form.chart_type.value };
  if (form.mentions.checked) request.retrieval = { name_matching: "mentioned" };
  return request;
}

// --- Asking ----------------------------------------------------------------------------------

// Validation errors name plan paths. These are the ones the form's fields set.
const FIELD_LABELS = {
  "filters.interventions": "Drug",
  "filters.conditions": "Condition",
  "filters.phases": "Phase",
  "filters.countries": "Country",
  "filters.start_year_from": "Started from",
  "filters.start_year_to": "Started up to",
  "presentation.chart_type": "Chart",
};

async function ask(request) {
  if (!request.query) {
    showProblem("Type a question first.", []);
    return;
  }
  const button = $("ask-button");
  button.disabled = true;
  $("result").hidden = true;
  showLoading();
  try {
    const response = await fetch("/v1/query", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(request),
    });
    const body = await response.json();
    if (!response.ok) {
      const error = body.error ?? { message: `The service answered ${response.status}.`, details: [] };
      showProblem(error.message, (error.details ?? []).map((d) => (d.path ? `${FIELD_LABELS[d.path] ?? d.path}: ${d.message}` : d.message)));
    } else if (body.status === "needs_clarification") {
      showClarification(body);
    } else {
      $("status").hidden = true;
      markSample(null);
      showAnswer(body, "Live answer");
    }
  } catch (error) {
    showProblem("The service could not be reached. Check that it is running and try again.", [String(error.message ?? error)]);
  } finally {
    button.disabled = false;
    clearInterval(showLoading.timer);
  }
}

function showLoading() {
  const status = $("status");
  const elapsed = el("span", {}, "");
  status.className = "status loading";
  const loader = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  loader.setAttribute("class", "pulse-loader");
  loader.setAttribute("viewBox", "0 0 64 28");
  loader.setAttribute("aria-hidden", "true");
  const trace = document.createElementNS("http://www.w3.org/2000/svg", "path");
  trace.setAttribute("d", "M2 15h16l4-8 6 16 5-12 3 4h26");
  loader.append(trace);
  status.replaceChildren(el("div", { class: "status-box" }, loader, el("span", {}, "Reading the question, querying ClinicalTrials.gov and citing the records. Large questions take up to a minute. ", elapsed)));
  status.hidden = false;
  const started = Date.now();
  clearInterval(showLoading.timer);
  showLoading.timer = setInterval(() => (elapsed.textContent = `${Math.round((Date.now() - started) / 1000)}s`), 1000);
}

function showProblem(message, details) {
  const status = $("status");
  status.className = "status problem";
  status.replaceChildren(el("div", { class: "status-box" }, el("h3", {}, "That did not work"), el("p", {}, message), ...(details.length ? [el("ul", {}, details.map((d) => el("li", {}, d)))] : [])));
  status.hidden = false;
}

function showClarification(body) {
  const status = $("status");
  status.className = "status clarify";
  const missing = body.missing?.length ? [el("p", {}, `Fields that would settle it: ${body.missing.join(", ")}`)] : [];
  status.replaceChildren(el("div", { class: "status-box" }, el("h3", {}, "One more detail"), el("p", {}, body.message), ...missing));
  status.hidden = false;
}

// --- Answer ----------------------------------------------------------------------------------

function showAnswer(answer, kind) {
  const views = [answer.visualization, ...answer.alternatives].map((view) => ({
    view,
    data: view.data ?? answer.visualization.data,
  }));
  current = { answer, views, active: 0 };

  $("result-title").textContent = answer.visualization.title;
  $("result-subtitle").textContent = answer.visualization.subtitle ?? "";
  $("result-subtitle").hidden = !answer.visualization.subtitle;
  const when = formatDate(answer.meta.data_version);
  $("result-kind").textContent = `${kind}, registry data as of ${when}`;
  $("result-facts").replaceChildren(
    ...answer.meta.cohorts.map((c) => el("li", {}, c.label ? `${c.label}: ` : "", el("strong", {}, number(c.total)), " trials")),
    el("li", {}, el("strong", {}, String(views.length)), views.length === 1 ? " view" : " views"),
  );

  const tabs = $("views");
  tabs.replaceChildren(
    ...views.map(({ view }, index) =>
      el("button", { type: "button", role: "tab", id: `view-tab-${index}`, "aria-controls": "chart-panel", "aria-selected": String(index === 0), tabindex: index === 0 ? "0" : "-1", onclick: () => selectView(index), onkeydown: (event) => moveTab(event, index) },
        CHART_LABELS[view.type] ?? view.type, index === 0 ? el("span", { class: "tag" }, "Recommended") : null),
    ),
  );

  renderReading(answer.meta);
  renderNotes(answer.meta.notes);
  renderMentions(answer.meta.mentions);
  $("requests").replaceChildren(...answer.meta.registry_requests.map((url) => el("li", {}, el("a", { href: url, target: "_blank", rel: "noopener" }, url))));

  $("result").hidden = false;
  selectView(0);
  $("result").scrollIntoView({ behavior: "smooth", block: "start" });
  $("result").focus({ preventScroll: true });
}

// Arrow keys move between views, as in any tab list.
function moveTab(event, index) {
  const count = current.views.length;
  const next = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: count - 1 }[event.key];
  if (next === undefined) return;
  event.preventDefault();
  const target = (next + count) % count;
  selectView(target);
  $(`view-tab-${target}`).focus();
}

const formatDate = (iso) => new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });

function selectView(index) {
  current.active = index;
  [...$("views").children].forEach((tab, i) => {
    tab.setAttribute("aria-selected", String(i === index));
    tab.setAttribute("tabindex", i === index ? "0" : "-1");
  });
  $("chart-panel").setAttribute("aria-labelledby", `view-tab-${index}`);
  const { view, data } = current.views[index];
  const reason = $("view-reason");
  reason.replaceChildren(el("strong", {}, index === 0 ? "Why this view: " : "Also fits: "), view.reason, ".");
  resetEvidence();
  renderView(view, data).catch((error) => {
    $("chart").replaceChildren(el("p", { class: "empty" }, `This view could not be drawn: ${error.message}`));
  });
}

// --- Renderers -------------------------------------------------------------------------------

let renderCount = 0;

// Charts drawn by lazily loaded libraries finish after an await. A later view switch
// bumps the count, and the stale render stops instead of drawing over the new view.
const stale = (token) => token !== renderCount;

async function renderView(view, data) {
  const chart = $("chart");
  const token = ++renderCount;
  // Hold the frame's height while the next view loads, so the page does not jump.
  chart.style.minHeight = `${chart.offsetHeight}px`;
  try {
    await drawView(chart, view, data, token);
  } finally {
    if (!stale(token)) chart.style.minHeight = "";
  }
}

async function drawView(chart, view, data, token) {
  chart.replaceChildren();
  $("chart-hint").hidden = view.type === "stat";
  const empty = Array.isArray(data) ? data.length === 0 : !(data.nodes?.length);
  if (empty && view.type !== "stat") {
    chart.append(el("p", { class: "empty" }, "No trials match this question."));
    return;
  }
  if (view.type === "stat") return renderStat(chart, data);
  if (view.type === "table") return renderTable(chart, view, data);
  if (view.type === "network_graph") return renderNetwork(chart, data, false, token);
  if (view.type === "network_graph_3d") return renderNetwork(chart, data, true, token);
  if (["treemap", "sankey", "scatter_plot_3d"].includes(view.type)) return renderPlotly(chart, view, data, token);
  if (view.vega_lite) return renderVega(chart, view, data);
  return renderTable(chart, view, data);
}

function vegaConfig() {
  const ink = css("--ink");
  const muted = css("--muted");
  const line = css("--line");
  return {
    background: "transparent",
    font: "Inter, system-ui, sans-serif",
    view: { stroke: null },
    axis: { labelColor: muted, titleColor: ink, gridColor: line, domainColor: css("--line-strong"), tickColor: line, labelFontSize: 12, titleFontSize: 12.5, titleFontWeight: 500, titlePadding: 10 },
    axisBand: { grid: false },
    legend: { labelColor: muted, titleColor: ink, labelFontSize: 12, titleFontSize: 12.5, titleFontWeight: 500 },
    title: { color: ink, fontSize: 0 },
    range: { category: SERIES, ordinal: RAMP.slice(1), ramp: RAMP, heatmap: RAMP },
    mark: { color: SERIES[0] },
    bar: { color: SERIES[0], cornerRadiusEnd: 4, stroke: css("--surface"), strokeWidth: 1 },
    rect: { stroke: css("--surface"), strokeWidth: 2 },
    line: { color: SERIES[0], strokeWidth: 2 },
    area: { color: SERIES[0], opacity: 0.85 },
    point: { color: SERIES[0], size: 70, filled: true },
    circle: { color: SERIES[0], opacity: 0.75, stroke: css("--surface"), strokeWidth: 1 },
  };
}

async function renderVega(chart, view, data) {
  const spec = structuredClone(view.vega_lite);
  delete spec.title;
  const isMap = view.type === "choropleth_map";
  const rows = Array.isArray(data) ? data.length : 0;
  if (!isMap) {
    spec.width = "container";
    spec.autosize = { type: "fit-x", contains: "padding" };
  } else {
    spec.width = Math.max(320, chart.clientWidth - 8);
    spec.autosize = { type: "fit", contains: "padding" };
    spec.layer[0].mark.fill = css("--line"); // countries with no trials follow the theme
  }
  const tall = view.type === "horizontal_bar_chart" || view.type === "timeline";
  const shown = view.options?.display_limit ? Math.min(rows, view.options.display_limit) : rows;
  spec.height = tall ? Math.max(260, Math.min(shown, 60) * 24) : view.type === "heatmap" ? 380 : 360;
  if (isMap) {
    // The Equal Earth projection is about twice as wide as it is tall. On a phone the
    // legend goes underneath, so the map keeps the full width.
    const narrow = chart.clientWidth < 560;
    spec.height = Math.round(spec.width * 0.5) + (narrow ? 70 : 0);
    if (narrow) spec.layer[1].encoding.color.legend = { orient: "bottom", direction: "horizontal", gradientLength: Math.min(240, spec.width - 40) };
  }
  // Embedding into a child, not the frame itself, means a stale render that finishes late
  // only fills a node the next view has already removed.
  const host = el("div");
  chart.append(host);
  const config = vegaConfig();
  // On a phone a side legend takes a third of the plot, so it goes underneath instead.
  if (chart.clientWidth < 560) config.legend = { ...config.legend, orient: "bottom", direction: "horizontal" };
  const result = await vegaEmbed(host, spec, { actions: false, renderer: "svg", config });
  result.view.addEventListener("click", (_event, item) => {
    const datum = item?.datum;
    if (!datum) return;
    const row = matchRow(data, datum);
    if (row) showEvidence(row, view);
  });
}

function sameValue(rowValue, datumValue) {
  if (rowValue === datumValue) return true;
  if (datumValue instanceof Date || (typeof datumValue === "number" && typeof rowValue === "string")) {
    const parsed = Date.parse(rowValue);
    const target = datumValue instanceof Date ? datumValue.getTime() : datumValue;
    return Number.isFinite(parsed) && parsed === target;
  }
  return false;
}

function matchRow(rows, datum) {
  if (!Array.isArray(rows)) return null;
  const source = datum.properties ? { ...datum, ...datum.properties } : datum;
  let best = null;
  let bestScore = 0;
  for (const row of rows) {
    let score = 0;
    let conflict = false;
    for (const [key, value] of Object.entries(row)) {
      if (HEAVY.has(key) || value === null || typeof value === "object" || !(key in source)) continue;
      if (sameValue(value, source[key])) score += 1;
      else conflict = true;
    }
    if (!conflict && score > bestScore) {
      best = row;
      bestScore = score;
    }
  }
  return best;
}

function renderStat(chart, data) {
  const [row] = data;
  chart.append(
    el("div", { class: "stat" },
      el("div", { class: "stat-number" }, number(row.trial_count)),
      el("div", { class: "stat-label" }, "trials match"),
      el("button", { type: "button", class: "button secondary", onclick: () => showEvidence(row, { type: "stat" }) }, "Show the evidence")),
  );
}

function tableColumns(view, data) {
  if (Array.isArray(data)) {
    if (view.options?.columns) return view.options.columns;
    const keys = Object.keys(data[0] ?? {}).filter((k) => !HEAVY.has(k) && k !== "key" && k !== "iso_a3" && k !== "iso_numeric");
    return keys;
  }
  return data.edges ? ["source", "target", "trial_count"] : ["source", "target", "trial_count"];
}

function renderTable(chart, view, data) {
  const labels = {};
  let rows = data;
  if (!Array.isArray(data)) {
    const nodes = Object.fromEntries(data.nodes.map((n) => [n.id, n.stage_label ? `${n.stage_label}: ${n.label}` : n.label]));
    rows = (data.edges ?? data.links).map((link) => ({ ...link, source: nodes[link.source], target: nodes[link.target] }));
    labels.source = "From";
    labels.target = "To";
  }
  const columns = tableColumns(view, data);
  const head = el("tr", {}, columns.map((c) => el("th", {}, labels[c] ?? c.replaceAll("_", " "))));
  const body = rows.map((row, index) => {
    const original = Array.isArray(data) ? row : (data.edges ?? data.links)[index];
    const cells = columns.map((c) => {
      const value = row[c];
      if (c === "nct_id") return el("td", {}, el("a", { href: `https://clinicaltrials.gov/study/${value}`, target: "_blank", rel: "noopener" }, value));
      return el("td", { class: typeof value === "number" ? "number" : undefined }, value === null || value === undefined ? "" : number(value));
    });
    const pick = () => showEvidence(original, view);
    return el("tr", { class: "selectable", tabindex: "0", onclick: pick, onkeydown: (event) => (event.key === "Enter" || event.key === " ") && (event.preventDefault(), pick()) }, cells);
  });
  chart.append(el("div", { class: "table-scroll" }, el("table", { class: "data" }, el("thead", {}, head), el("tbody", {}, body))));
}

// The same 0, 1, 2, 5, 10, 20, 50 ticks the backend puts on symlog axes.
function niceTicks(top) {
  const ticks = [0];
  for (let step = 1; step <= top; step *= 10) {
    for (const m of [1, 2, 5]) if (m * step <= top) ticks.push(m * step);
  }
  return ticks;
}

function shortNumber(value) {
  return value >= 1000 ? `${value / 1000}k` : String(value);
}

async function renderPlotly(chart, view, data, token) {
  await loadScript("plotly");
  if (stale(token)) return;
  const ink = css("--ink");
  const layout = {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: "Inter, system-ui, sans-serif", color: ink, size: 12 },
    margin: { l: 10, r: 10, t: 10, b: 10 },
    height: 520,
    colorway: palette(),
  };
  let traces;
  let rowFor;
  if (view.type === "treemap") {
    const field = view.encoding.label.field;
    traces = [{ type: "treemap", labels: data.map((r) => r[field]), parents: data.map(() => ""), values: data.map((r) => r.trial_count), textinfo: "label+value", marker: { colors: data.map((_, i) => palette()[i % 9]) } }];
    rowFor = (point) => data[point.pointNumber];
  } else if (view.type === "sankey") {
    const index = Object.fromEntries(data.nodes.map((n, i) => [n.id, i]));
    traces = [{
      type: "sankey",
      arrangement: "snap",
      node: { label: data.nodes.map((n) => n.label), pad: 14, thickness: 16, color: data.nodes.map((n) => palette()[n.stage % 9]), line: { width: 0 } },
      link: { source: data.links.map((l) => index[l.source]), target: data.links.map((l) => index[l.target]), value: data.links.map((l) => l.trial_count), color: "rgba(42,120,214,0.2)" },
    }];
    // A link click carries its source and target nodes. A node click does not.
    rowFor = (point) => (point.source && point.target ? data.links[point.pointNumber] : data.nodes[point.pointNumber]);
  } else {
    const x = view.encoding.x.field;
    const y = view.encoding.y.field;
    const z = view.encoding.z.field;
    const color = view.encoding.color?.field;
    const groups = color ? [...new Set(data.map((r) => r[color]))] : [null];
    // Plotly has no symlog axis. A skewed measure is plotted as log10(1 + value), with
    // ticks labeled in the original units, which is the same curve Vega's symlog draws.
    const skewed = (channel) => channel.scale === "symlog";
    const position = (channel) => (skewed(channel) ? (v) => Math.log10(1 + v) : (v) => v);
    const [px, py, pz] = [view.encoding.x, view.encoding.y, view.encoding.z].map(position);
    traces = groups.map((group, i) => {
      const rows = data.filter((r) => !color || r[color] === group);
      return { type: "scatter3d", mode: "markers", name: group ?? "Trials", x: rows.map((r) => px(r[x])), y: rows.map((r) => py(r[y])), z: rows.map((r) => pz(r[z])), text: rows.map((r) => `${r.title}<br>${number(r[x])} · ${number(r[y])} · ${number(r[z])}`), customdata: rows.map((r) => data.indexOf(r)), hovertemplate: "%{text}<extra></extra>", marker: { size: 3.5, opacity: 0.8, color: palette()[i % 9] } };
    });
    const axis = (channel) => {
      const base = { title: { text: channel.title }, type: "linear", gridcolor: css("--line"), color: css("--muted") };
      if (!skewed(channel)) return base;
      const top = Math.max(...data.map((r) => r[channel.field] ?? 0));
      const ticks = niceTicks(top);
      return { ...base, tickvals: ticks.map((t) => Math.log10(1 + t)), ticktext: ticks.map(shortNumber) };
    };
    layout.scene = { xaxis: axis(view.encoding.x), yaxis: axis(view.encoding.y), zaxis: axis(view.encoding.z) };
    layout.margin = { l: 0, r: 0, t: 0, b: 0 };
    rowFor = (point) => data[point.customdata];
  }
  // Wheel zoom off, so scrolling the page past a 3D scene does not zoom it instead.
  await Plotly.newPlot(chart, traces, layout, { displaylogo: false, responsive: true, scrollZoom: false, modeBarButtonsToRemove: ["toImage"] });
  chart.on("plotly_click", (event) => {
    const point = event.points?.[0];
    const row = point && rowFor(point);
    if (row) showEvidence(row, view);
  });
}

async function renderNetwork(chart, data, threeD, token) {
  if (threeD) await loadScript("graph3d");
  if (stale(token)) return;
  const colors = palette();
  const labels = Object.fromEntries(data.nodes.map((n) => [n.id, n.label]));
  const maxCount = Math.max(...data.nodes.map((n) => n.trial_count));
  const scale = threeD ? 220 : 260;
  const nodes = data.nodes.map((n) => ({
    ...n,
    fx: (threeD ? n.x3 : n.x) * scale,
    fy: (threeD ? n.y3 : n.y) * scale,
    ...(threeD ? { fz: n.z3 * scale } : {}),
    val: 1 + (12 * n.trial_count) / maxCount,
  }));
  const links = data.edges.map((e) => ({ ...e }));
  const width = chart.clientWidth;
  // Roughly square on a phone, so the graph is not a small cluster in a tall empty frame.
  const height = Math.min(threeD ? 560 : 520, Math.max(320, Math.round(width * 1.05)));
  const nodeLabel = (n) => `${n.label} (${n.trial_count} trials)`;
  const color = (n) => colors[n.community % colors.length];
  const linkWidth = (l) => 0.5 + Math.log2(1 + l.trial_count);
  const pickNode = (n) => showEvidence(data.nodes.find((x) => x.id === n.id), { type: "network" }, labels);
  const pickLink = (l) => showEvidence(data.edges.find((e) => e.source === (l.source.id ?? l.source) && e.target === (l.target.id ?? l.target)), { type: "network" }, labels);

  if (threeD) {
    const graph = ForceGraph3D()(chart)
      .width(width).height(height)
      .backgroundColor("#ffffff")
      .graphData({ nodes, links })
      .nodeLabel(nodeLabel).nodeVal("val").nodeColor(color).nodeOpacity(0.92)
      .linkWidth(linkWidth).linkColor(() => css("--faint")).linkOpacity(0.35)
      .onNodeClick(pickNode).onLinkClick(pickLink);
    graph.cameraPosition({ z: 520 });
  } else {
    const graph = ForceGraph()(chart)
      .width(width).height(height)
      .graphData({ nodes, links })
      .nodeLabel(nodeLabel).nodeVal("val").nodeColor(color)
      .linkWidth(linkWidth).linkColor(() => css("--line-strong"))
      .nodeCanvasObjectMode(() => "after")
      .nodeCanvasObject((node, ctx, zoom) => {
        if (node.val < 4 && zoom < 2.2) return;
        ctx.font = `${11 / zoom}px Inter, sans-serif`;
        ctx.fillStyle = css("--ink");
        ctx.textAlign = "center";
        ctx.fillText(node.label.length > 26 ? `${node.label.slice(0, 25)}…` : node.label, node.x, node.y + Math.sqrt(node.val) * 4 + 9 / zoom);
      })
      .onNodeClick(pickNode).onLinkClick(pickLink)
      .cooldownTicks(0);
    setTimeout(() => graph.zoomToFit(400, 30), 50);
  }
  const types = [...new Set(data.nodes.map((n) => n.type.replaceAll("_", " ")))];
  chart.append(el("div", { class: "graph-legend" }, `${data.nodes.length} ${types.join(" and ")} nodes, ${data.edges.length} links. Colors are clusters of entities that share many trials. ${threeD ? "Drag to rotate" : "Drag to pan"}, scroll to zoom.`));
}

// --- Evidence --------------------------------------------------------------------------------

function resetEvidence() {
  $("evidence-body").replaceChildren(el("p", { class: "muted" }, "Every number here comes from ClinicalTrials.gov records. Select part of the chart to see which trials it counts, and the exact field and value each one was counted by."));
}

function rowLabel(row, labels) {
  if (row.source && row.target && labels) return `${labels[row.source] ?? row.source} and ${labels[row.target] ?? row.target}`;
  if (row.title) return row.title;
  const skip = new Set([...HEAVY, "trial_count", "key", "iso_a3", "iso_numeric", "bin_start", "bin_end", "n", "min", "q1", "median", "q3", "max", "whisker_low", "whisker_high", "mean", "small_sample", "degree", "community", "x", "y", "x3", "y3", "z3", "stage", "id", "type"]);
  const parts = Object.entries(row).filter(([k, v]) => !skip.has(k) && typeof v === "string").map(([, v]) => v);
  return parts.join(", ") || row.label || "Selected";
}

function showEvidence(row, view, labels) {
  if (!row) return;
  const body = $("evidence-body");
  const parts = [el("p", { class: "selected" }, rowLabel(row, labels))];

  if (typeof row.trial_count === "number") {
    parts.push(el("p", { class: "count" }, `${number(row.trial_count)} trial${row.trial_count === 1 ? "" : "s"}`));
    const listed = row.nct_ids?.length ?? 0;
    if (listed && row.trial_count > listed) {
      parts.push(el("p", { class: "note" }, `Counted directly by the registry. The ${number(listed)} trials fetched for this answer are listed and cited below.`));
    }
  }
  if ("median" in row) {
    parts.push(el("dl", { class: "stats" },
      ["n", "min", "q1", "median", "q3", "max", "mean"].flatMap((k) => [el("dt", {}, k), el("dd", {}, number(row[k]))])));
    if (row.small_sample) parts.push(el("p", { class: "note" }, "Fewer than 5 values, so this box is only a rough guide."));
  }
  if (row.nct_id && !row.trial_count) {
    const measures = Object.entries(row).filter(([k, v]) => typeof v === "number" && !["x", "y"].includes(k));
    if (measures.length) parts.push(el("dl", { class: "stats" }, measures.flatMap(([k, v]) => [el("dt", {}, k.replaceAll("_", " ")), el("dd", {}, number(v))])));
  }

  const citations = row.citations ?? [];
  if (citations.length) {
    parts.push(el("h4", {}, "Why these trials are counted here"));
    for (const citation of citations) {
      parts.push(el("div", { class: "citation" },
        el("a", { href: citation.url, target: "_blank", rel: "noopener" }, citation.nct_id),
        el("div", { class: "field" }, citation.field),
        el("span", { class: `excerpt${citation.kind === "quote" ? " quote" : ""}` }, citation.kind === "quote" ? `“${citation.excerpt}”` : citation.excerpt)));
    }
  }

  const ids = row.nct_ids ?? (row.nct_id ? [row.nct_id] : []);
  if (ids.length > citations.length) {
    parts.push(el("h4", {}, `All ${number(ids.length)} trials`));
    const shown = ids.slice(0, 60);
    parts.push(el("ul", { class: "trial-list" }, shown.map((id) => el("li", {}, el("a", { href: `https://clinicaltrials.gov/study/${id}`, target: "_blank", rel: "noopener" }, id)))));
    if (ids.length > shown.length) parts.push(el("p", { class: "muted" }, `and ${number(ids.length - shown.length)} more in the response data.`));
  }
  body.replaceChildren(...parts);
  if (window.matchMedia("(max-width: 960px)").matches) $("evidence").scrollIntoView({ behavior: "smooth", block: "start" });
}

// --- Reading, notes, mentions ----------------------------------------------------------------

// Registry codes read as words: "PHASE3" as "Phase 3", "ACTIVE_NOT_RECRUITING" as
// "Active not recruiting". Names people typed are left exactly as typed.
function humanize(value) {
  const text = String(value);
  if (!/^[A-Z0-9_]+$/.test(text)) return text.replaceAll("_", " ");
  const words = text.replace(/^PHASE(\d)$/, "PHASE $1").replace(/^EARLY_PHASE1$/, "EARLY PHASE 1").replaceAll("_", " ").toLowerCase();
  return words === "na" ? "Not applicable" : words === "nih" ? "NIH" : words[0].toUpperCase() + words.slice(1);
}

function describeValue(value) {
  if (Array.isArray(value)) return value.map((v) => (typeof v === "object" ? v.label ?? JSON.stringify(v) : humanize(v))).join(", ");
  if (typeof value === "object" && value !== null) return JSON.stringify(value);
  return humanize(value);
}

function renderReading(meta) {
  const plan = meta.plan;
  const rows = [];
  const add = (path, label, value) => {
    if (value === null || value === undefined) return;
    const source = meta.provenance[path];
    rows.push(el("dt", {}, label), el("dd", {}, describeValue(value), source ? el("span", { class: `source ${source}` }, SOURCE_LABELS[source] ?? source) : null));
  };
  const filters = plan.filters;
  const filterLabels = { conditions: "Conditions", interventions: "Drugs", sponsors: "Lead sponsors", keywords: "Keywords", phases: "Phases", statuses: "Statuses", sponsor_classes: "Sponsor classes", intervention_types: "Intervention types", study_types: "Study types", countries: "Countries", start_year_from: "Started from", start_year_to: "Started up to" };
  for (const [key, label] of Object.entries(filterLabels)) add(`filters.${key}`, label, filters[key]);
  add("analysis.kind", "Analysis", plan.analysis.kind);
  for (const [key, value] of Object.entries(plan.analysis)) {
    if (key === "kind" || value === null) continue;
    if (key === "cohorts") add("analysis.cohorts", "Compared", value.map((c) => c.label));
    else add(`analysis.${key}`, key.replaceAll("_", " "), value);
  }
  add("presentation.chart_type", "Chart asked for", plan.presentation.chart_type);
  add("presentation.top_n", "Show top", plan.presentation.top_n);
  add("retrieval.name_matching", "Names must be", plan.retrieval?.name_matching === "mentioned" ? "named or mentioned" : "named as an intervention or condition");
  if (meta.interpreter) rows.push(el("dt", {}, "Read by"), el("dd", {}, meta.interpreter.startsWith("claude") ? "Claude" : "rule-based interpreter"));
  $("reading").replaceChildren(...rows);
}

function renderNotes(notes) {
  $("notes").replaceChildren(...notes.map((note) => el("li", {}, note.message)));
}

function renderMentions(reports) {
  const section = $("mentions");
  if (!reports.length) {
    section.hidden = true;
    return;
  }
  const parts = [];
  for (const report of reports) {
    const who = report.cohort ? `${report.cohort}: ` : "";
    const verb = report.counted ? "are counted in the chart" : "are not counted in the chart";
    parts.push(el("p", {}, `${who}${number(report.total)} trials mention the drug or condition only in their descriptions, and ${verb}. Some examples:`));
    for (const citation of report.citations.slice(0, 4)) {
      parts.push(el("blockquote", {}, `“${citation.excerpt}”`, el("cite", {}, el("a", { href: citation.url, target: "_blank", rel: "noopener" }, citation.nct_id), ` ${citation.field}`)));
    }
  }
  $("mentions-body").replaceChildren(...parts);
  section.hidden = false;
}

// --- Samples and the hero chart ---------------------------------------------------------------

const REQUEST_LABELS = { drug_name: "Drug", condition: "Condition", trial_phase: "Phase", sponsor: "Sponsor", country: "Country", start_year: "Started from", end_year: "Started up to" };

async function loadSamples() {
  const grid = $("sample-grid");
  let samples;
  try {
    const response = await fetch("/v1/samples");
    samples = await response.json();
  } catch {
    grid.replaceChildren(el("li", { class: "empty" }, "The saved samples could not be loaded."));
    return;
  }
  if (!samples.length) {
    $("samples").hidden = true;
    return;
  }
  if (PAGE === "samples") {
    $("samples-intro").textContent = `These questions ran against the live registry on ${formatDate(samples[0].data_version)}, and their full responses are saved with the project, so they open instantly and cost nothing to explore.`;
    grid.replaceChildren(...samples.map(sampleCard));
  } else {
    drawHero(samples[0]);
  }
}

function icon(id) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#${id}`);
  svg.append(use);
  return svg;
}

function sampleCard(sample) {
  const [symbol, tone] = SAMPLE_LOOK[sample.chart_type] ?? ["i-chart", 1];
  const { query, ...fields } = sample.request;
  const pinned = Object.entries(fields).map(([key, value]) => `${REQUEST_LABELS[key] ?? key}: ${value}`).join(", ");
  const totals = sample.cohorts.map((c) => number(c.total));
  return el("li", {},
    el("button", { type: "button", class: "sample-card", "data-sample": sample.name, onclick: () => openSample(sample.name) },
      el("span", { class: "sample-top" },
        el("span", { class: `sample-icon tone-${tone}` }, icon(symbol)),
        el("span", { class: "chip" }, CHART_LABELS[sample.chart_type] ?? sample.chart_type)),
      el("span", { class: "sample-question" }, query),
      pinned ? el("span", { class: "sample-fields" }, pinned) : null,
      el("span", { class: "sample-foot" },
        el("span", { class: "sample-total" }, el("strong", {}, totals.join(" and ")), totals.length > 1 ? "trials compared" : "trials"),
        el("span", { class: "sample-open" }, "Open", icon("i-arrow")))));
}

function markSample(name) {
  document.querySelectorAll(".sample-card").forEach((card) => card.setAttribute("aria-current", String(card.dataset.sample === name)));
}

async function openSample(name) {
  try {
    const response = await fetch(`/v1/samples/${encodeURIComponent(name)}`);
    if (!response.ok) throw new Error(`The service answered ${response.status}.`);
    const answer = await response.json();
    $("status").hidden = true;
    markSample(name);
    showAnswer(answer, "Saved answer");
  } catch (error) {
    showProblem("This sample could not be opened.", [String(error.message ?? error)]);
  }
}

// The hero draws the first sample's trend as a small area chart and quotes one real citation
// from its busiest year, so the first thing on the page is the product working on real data.
async function drawHero(sample) {
  let answer;
  try {
    answer = await (await fetch(`/v1/samples/${encodeURIComponent(sample.name)}`)).json();
  } catch {
    return;
  }
  // Only complete years: the current year is partial, and the registry also lists trials
  // planned to start later, so the last points would dip for reasons the preview cannot show.
  const dataYear = new Date(sample.data_version).getFullYear();
  const all = Array.isArray(answer.visualization.data) ? answer.visualization.data : [];
  const rows = all.filter((row) => Number(row.period) < dataYear);
  if (answer.visualization.type !== "time_series" || rows.length < 3) return;

  const width = 520, height = 210, left = 8, right = 8, top = 16, bottom = 26;
  const peak = rows.reduce((best, row) => (row.trial_count > best.trial_count ? row : best), rows[0]);
  const x = (i) => left + (i / (rows.length - 1)) * (width - left - right);
  const y = (v) => top + (1 - v / peak.trial_count) * (height - top - bottom);
  const points = rows.map((row, i) => `${x(i).toFixed(1)},${y(row.trial_count).toFixed(1)}`);
  const peakIndex = rows.indexOf(peak);
  const ns = "http://www.w3.org/2000/svg";
  const node = (tag, attrs) => {
    const n = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  };
  const svg = node("svg", { viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none", "aria-hidden": "true" });
  const gradient = node("linearGradient", { id: "hero-fill", x1: "0", y1: "0", x2: "0", y2: "1" });
  gradient.append(node("stop", { offset: "0%", "stop-color": SERIES[0], "stop-opacity": "0.28" }), node("stop", { offset: "100%", "stop-color": SERIES[0], "stop-opacity": "0.02" }));
  const defs = node("defs", {});
  defs.append(gradient);
  const base = height - bottom;
  svg.append(
    defs,
    node("line", { x1: left, x2: width - right, y1: base, y2: base, stroke: css("--line-strong"), "stroke-width": "1" }),
    node("polygon", { points: `${x(0)},${base} ${points.join(" ")} ${x(rows.length - 1)},${base}`, fill: "url(#hero-fill)" }),
    node("polyline", { points: points.join(" "), fill: "none", stroke: SERIES[0], "stroke-width": "2.4", "stroke-linejoin": "round", "vector-effect": "non-scaling-stroke" }),
    node("circle", { cx: x(peakIndex), cy: y(peak.trial_count), r: "5", fill: css("--coral"), stroke: "#fff", "stroke-width": "2" }),
  );
  for (const i of [0, Math.floor((rows.length - 1) / 2), rows.length - 1]) {
    const label = node("text", { x: x(i), y: height - 6, class: "axis", "text-anchor": i === 0 ? "start" : i === rows.length - 1 ? "end" : "middle" });
    label.textContent = rows[i].period;
    svg.append(label);
  }
  const spark = $("hero-spark");
  spark.replaceChildren(svg);
  spark.setAttribute("aria-label", `Line chart of trials started per year from ${rows[0].period} to ${rows.at(-1).period}, peaking at ${number(peak.trial_count)} trials in ${peak.period}.`);

  $("hero-card-title").textContent = answer.visualization.title;
  $("hero-card-sub").textContent = `${number(sample.cohorts[0].total)} trials, saved ${formatDate(sample.data_version)}, complete years shown`;
  const citation = peak.citations?.[0];
  if (citation) {
    const field = citation.field.split(".").slice(-2).join(".");
    $("hero-cite").replaceChildren(
      el("span", { class: "cite-icon", "aria-hidden": "true" }, icon("i-cite")),
      el("span", {}, `${peak.period} has ${number(peak.trial_count)} trials. One of them is `,
        el("a", { href: citation.url, rel: "noopener" }, citation.nct_id),
        ", whose ", el("code", {}, field), ` reads ${citation.excerpt}.`),
    );
  }
}

// The page is chosen by an inline script in index.html before anything renders.
const PAGE = document.body.dataset.page;
document.querySelector(`[data-nav="${PAGE}"]`)?.setAttribute("aria-current", "page");

setUpForm();
loadSamples();
