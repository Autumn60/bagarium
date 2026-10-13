// Keep job cards ([data-job-id]) current: update progress of running jobs and
// reload the page when any shown job changes state (queued -> running -> done).
(() => {
  const cards = [...document.querySelectorAll("[data-job-id]")]
    .filter(c => c.dataset.state === "queued" || c.dataset.state === "running");
  if (!cards.length) return;
  const set = (card, field, fn) => { const el = card.querySelector(`[data-f="${field}"]`); if (el) fn(el); };
  const poll = async () => {
    let jobs;
    try { jobs = await (await fetch("/api/jobs")).json(); } catch { return setTimeout(poll, 2000); }
    const byId = new Map(jobs.map(j => [String(j.id), j]));
    for (const card of cards) {
      const j = byId.get(card.dataset.jobId);
      if (!j || j.state !== card.dataset.state || (j.cancelling ? "true" : "") !== card.dataset.cancelling) {
        return location.reload();
      }
      if (j.state !== "running") continue;
      set(card, "progress-text", el => el.textContent = j.kind === "scan"
        ? `${j.done} / ${j.total}` : `${Math.round(100 * j.done / (j.total || 1))}%`);
      set(card, "progress", el => { el.max = j.total || 1; el.value = j.done; });
      set(card, "current", el => el.textContent = j.current || "starting…");
      set(card, "stats", el => el.textContent =
        Object.entries(j.stats).map(([k, v]) => `${k} ${v.toLocaleString()}`).join(" · "));
    }
    setTimeout(poll, 1000);
  };
  setTimeout(poll, 1000);
})();
