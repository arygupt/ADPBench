// Display the reviewed request limits, never mislabel a finite API as unlimited.
const valid = n => Number.isSafeInteger(n) && n > 0;

export function outputLimit(meta, model) {
  const limits = meta?.max_output_tokens;
  const value = meta?.output_budget === "provider_max" ? limits?.[model] : limits;
  return valid(value) ? value : null;
}

export function budgetSummary(meta) {
  if (meta?.output_budget === "provider_max") {
    const limits = meta.max_output_tokens;
    const values = limits && typeof limits === "object" && !Array.isArray(limits) ? Object.values(limits) : [];
    if (!values.length || !values.every(valid)) return "Output limits unavailable";
    const low = Math.min(...values), high = Math.max(...values);
    return `Provider-maximum output budgets · ${low.toLocaleString("en-US")}${high === low ? "" : "–" + high.toLocaleString("en-US")} tokens/request, depending on model`;
  }
  const limit = outputLimit(meta);
  return limit ? `${limit.toLocaleString("en-US")} output-token cap/request` : "Output limit unavailable";
}
