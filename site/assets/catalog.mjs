// Only reviewed local dataset paths may become fetch targets.
export function parseCatalog(value) {
  if (value?.schema_version !== 1 || !Array.isArray(value.evaluations) ||
      value.evaluations.length < 1 || value.evaluations.length > 100)
    throw new Error("Invalid evaluation catalog");
  const paths = Object.create(null);
  for (const item of value.evaluations) {
    if (!item || !/^[a-z0-9][a-z0-9-]{0,79}$/.test(item.id) ||
        typeof item.label !== "string" || !item.label || item.label.length > 200 ||
        !["single-shot", "iterative"].includes(item.protocol) || Object.hasOwn(paths, item.id) ||
        item.path !== (item.id === "pilot-001" ? "data/leaderboard.json" : `data/${item.id}/leaderboard.json`))
      throw new Error("Unsafe or duplicate evaluation entry");
    paths[item.id] = item.path;
  }
  if (!Object.hasOwn(paths, value.default)) throw new Error("Missing default evaluation");
  return { paths, entries: value.evaluations, defaultId: value.default };
}
