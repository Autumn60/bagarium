// Topic filter + checkbox helpers shared by the bag detail and batch extract pages.
(() => {
  const filter = document.getElementById("topic-filter");
  if (!filter) return;
  const rows = [...document.querySelectorAll("table.topics tbody tr")];
  const boxes = rows.map(r => r.querySelector("input[name=topic]")).filter(Boolean);
  const all = document.getElementById("check-all");
  const count = document.getElementById("selected-count");
  const visible = () => rows.filter(r => !r.hidden).map(r => r.querySelector("input[name=topic]")).filter(Boolean);
  const refresh = () => {
    if (!all) return;
    const n = boxes.filter(b => b.checked).length;
    if (count) count.textContent = n;
    all.checked = n === boxes.length;
    all.indeterminate = n > 0 && n < boxes.length;
  };
  filter.addEventListener("input", () => {
    const q = filter.value.trim().toLowerCase();
    rows.forEach(r => r.hidden = q && !r.dataset.search.includes(q));
  });
  all?.addEventListener("change", () => { boxes.forEach(b => b.checked = all.checked); refresh(); });
  document.getElementById("check-visible")?.addEventListener("click", () => { visible().forEach(b => b.checked = true); refresh(); });
  document.getElementById("uncheck-visible")?.addEventListener("click", () => { visible().forEach(b => b.checked = false); refresh(); });
  boxes.forEach(b => b.addEventListener("change", refresh));
})();
