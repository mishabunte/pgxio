// Browser regression check for a generated, nonempty benchmark report.
// Run with Aside, supplying its local URL:
// aside repl "const REPORT_URL='file:///absolute/path/report/charts.html'; $(cat benchmarks/tests/charts.browser.js)"
const pg = await openTab("about:blank");
await pg._sendToTarget("Page.addScriptToEvaluateOnNewDocument", {
  source: "window.__chartErrors=[];window.addEventListener('error',e=>window.__chartErrors.push(e.message));"
});
await pg.goto(REPORT_URL);
const result = await pg.evaluate(() => {
  const original = rows.slice();
  if (!original.length) throw Error("This check requires a nonempty generated report");
  const assert = (condition, message) => { if (!condition) throw Error(message); };
  const base = {...rows[0], cpus: 1, connections: 8};
  const trial = (profile_id, success_rps, overrides = {}) => ({...base, profile_id, success_rps, workload_passed: true, ...overrides});
  try {
    rows.splice(0, rows.length,
      trial("repeat-a", 10), trial("repeat-a", 30, {workload_passed: false}),
      trial("separate-b", 0), trial("missing-c", null),
      trial("connections-d", 50, {connections: 32}));
    by('view').value = 'bars';
    by('y').value = 'success_rps';
    draw();
    const bars = [...document.querySelectorAll('rect[data-profile]')];
    const repeated = bars.find(bar => bar.dataset.profile === 'repeat-a');
    assert(document.querySelectorAll('.panel').length === 2, 'Separate connection counts into panels');
    assert(bars.length === 3, 'Preserve exact profiles and omit unavailable metrics');
    assert(Number(repeated.dataset.median) === 20, 'Median of an even number of trials');
    assert(Number(repeated.dataset.min) === 10 && Number(repeated.dataset.max) === 30, 'Trial range');
    assert(repeated.getAttribute('opacity') === '0.55', 'Failed trial remains included and flagged');
    assert(bars.some(bar => bar.dataset.profile === 'separate-b' && Number(bar.dataset.median) === 0), 'Zero is a measurement');
    assert(by('status').textContent.includes('1 failed loads'), 'Expose failed load count');
    assert(by('table').children.length === 4, 'Unavailable profile remains in the table');
    by('view').value = 'scatter';
    by('x').value = 'cpus';
    draw();
    assert(!by('plot').hasAttribute('hidden'), 'Raw scatter must be visible');
    const dots = [...document.querySelectorAll('#plot circle')];
    assert(dots.length === 4 && dots.every(dot => Number(dot.getAttribute('cx')) === 526), 'Constant numeric axis is centered');
    by('x').value = 'connections';
    draw();
    assert(new Set([...document.querySelectorAll('#plot circle')].map(dot => dot.getAttribute('cx'))).size === 2, 'Variable scatter axis');
    rows.splice(0);
    by('view').value = 'bars';
    draw();
    assert(!document.querySelectorAll('.panel').length && by('status').textContent === 'No trials match these filters.', 'Empty data is reviewable');
    return 'PASS: panels, exact profiles, median, range, failed loads, zero/missing metrics, scatter, empty data';
  } finally {
    rows.splice(0, rows.length, ...original);
    by('view').value = 'bars';
    draw();
  }
});
console.log(result);
const errors = await pg.evaluate(() => window.__chartErrors);
console.log('CONSOLE_ERRORS=' + JSON.stringify(errors));
if (errors.length) throw Error('Browser errors occurred');
await closeTab(pg);
console.log('GSTACK_STEP_OK');
