// Parse data/evaluations.json, the list of published datasets.
// Only reviewed local dataset paths may become fetch targets.

const PROTOCOLS = ["single-shot", "iterative", "agent-assisted-v1", "agent-assisted-v2"];
const DATASET_ID = /^[a-z0-9][a-z0-9-]{0,79}$/;

function isValidEntry(item, seenPaths) {
  return (
    item &&
    DATASET_ID.test(item.id) &&
    typeof item.label === "string" &&
    item.label.length > 0 &&
    item.label.length <= 200 &&
    PROTOCOLS.includes(item.protocol) &&
    !Object.hasOwn(seenPaths, item.id) &&
    item.path === `data/${item.id}/leaderboard.json`
  );
}

/** Returns { paths: {id: path}, entries, defaultId }, or throws for an unsafe catalog. */
export function parseCatalog(value) {
  const entries = value?.evaluations;
  if (value?.schema_version !== 1 || !Array.isArray(entries) || entries.length < 1 || entries.length > 100) {
    throw new Error("Invalid evaluation catalog");
  }

  const paths = Object.create(null);
  for (const item of entries) {
    if (!isValidEntry(item, paths)) throw new Error("Unsafe or duplicate evaluation entry");
    paths[item.id] = item.path;
  }
  if (!Object.hasOwn(paths, value.default)) throw new Error("Missing default evaluation");
  return { paths, entries, defaultId: value.default };
}
