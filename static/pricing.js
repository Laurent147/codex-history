'use strict';
const $ = id => document.getElementById(id);
const money = value => new Intl.NumberFormat('en-US', {style:'currency', currency:'USD', minimumFractionDigits: value < 1 ? 2 : 0, maximumFractionDigits: 3}).format(value);
const modelName = value => value.replace(/^gpt-/, 'GPT-').replace(/(^|[-.])(\w)/g, (_, separator, letter) => `${separator}${letter.toUpperCase()}`);
function notice(text) { $('notice').textContent = text; $('notice').hidden = !text; }
async function load() {
  try {
    const response = await fetch('/api/pricing');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    $('basis').textContent = data.basis;
    $('checked-at').textContent = data.checked_at;
    $('source').href = data.source;
    const rows = Object.entries(data.models || {}).sort(([a], [b]) => a.localeCompare(b));
    $('pricing-rows').replaceChildren(...rows.map(([name, rates]) => {
      const row = document.createElement('tr');
      const longContext = rates.long_context;
      row.innerHTML = `<th scope="row"><strong>${modelName(name)}</strong><small>${name}</small></th><td>${money(rates.input)}</td><td>${money(rates.cached_input)}</td><td>${money(rates.output)}</td><td>${longContext ? `>${Number(longContext.threshold).toLocaleString()} input tokens · ${longContext.input_multiplier}× input · ${longContext.output_multiplier}× output` : 'Base rates only'}</td><td>${rates.source ? `<a href="${rates.source}" target="_blank" rel="noreferrer">Model details</a>` : '<span class="muted">Local rate</span>'}</td>`;
      return row;
    }));
    $('pricing-empty').hidden = rows.length > 0;
  } catch (error) { notice(error.message); $('pricing-empty').hidden = false; }
}
load();
