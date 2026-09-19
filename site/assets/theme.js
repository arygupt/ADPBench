// Apply the saved preference before paint, even when storage is unavailable.
try {
  document.documentElement.dataset.theme =
    localStorage.getItem("adpbench-theme") === "dark" ? "dark" : "light";
} catch (_) {
  document.documentElement.dataset.theme = "light";
}
