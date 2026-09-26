// Describe the reviewed output-token limits. Never label a finite API limit as unlimited.

const isPositiveInteger = (n) => Number.isSafeInteger(n) && n > 0;
const format = (n) => n.toLocaleString("en-US");

/** The per-request output limit for a model, or null if unknown. */
export function outputLimit(meta, model) {
  const limits = meta?.max_output_tokens;
  const value = meta?.output_budget === "provider_max" ? limits?.[model] : limits;
  return isPositiveInteger(value) ? value : null;
}

/** A one-line description of a dataset's output budget. */
export function budgetSummary(meta) {
  if (meta?.output_budget === "provider_max") {
    const limits = meta.max_output_tokens;
    const isMap = limits && typeof limits === "object" && !Array.isArray(limits);
    const values = isMap ? Object.values(limits) : [];
    if (!values.length || !values.every(isPositiveInteger)) return "Output limits unavailable";
    const low = Math.min(...values);
    const high = Math.max(...values);
    const range = high === low ? format(low) : `${format(low)}–${format(high)}`;
    return `Provider-maximum output budgets · ${range} tokens/request, depending on model`;
  }
  const limit = outputLimit(meta);
  return limit ? `${format(limit)} output-token cap/request` : "Output limit unavailable";
}
