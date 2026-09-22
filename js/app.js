(function () {
  const viewNames = ["topology", "navs", "locate", "remote", "settings"];
  const initialized = {};
  const sections = Array.from(document.querySelectorAll(".view"));
  const navButtons = Array.from(document.querySelectorAll(".nav-item"));

  function activate(view) {
    sections.forEach((section) => section.classList.toggle("active", section.id === `view-${view}`));
    navButtons.forEach((button) => button.classList.toggle("active", button.dataset.view === view));
    if (!initialized[view]) {
      initialized[view] = true;
      if (view === "topology") NetOps.initTopology();
      if (view === "navs") NetOps.initNavs();
      if (view === "locate") NetOps.initLocate();
      if (view === "remote") NetOps.initRemote();
      if (view === "settings") NetOps.initSettings();
    } else {
      if (view === "navs") NetOps.refreshNavs();
      if (view === "locate") NetOps.refreshLocate();
      if (view === "remote") NetOps.refreshRemote();
      if (view === "settings") NetOps.refreshSettings();
    }
    NetOps.refreshIcons();
    window.history.replaceState(null, "", `#${view}`);
  }

  navButtons.forEach((button) => {
    button.addEventListener("click", () => activate(button.dataset.view));
  });

  const initialView = viewNames.includes(window.location.hash.slice(1))
    ? window.location.hash.slice(1)
    : "topology";
  activate(initialView);

  function updateCollectStatus() {
    NetOps.api("/api/collect/status")
      .then((status) => {
        const pill = document.getElementById("collectStatus");
        const label = pill.querySelector("b");
        const last = status.lastRuns && status.lastRuns[0];
        pill.classList.toggle("running", Boolean(status.running));
        if (status.running) {
          label.textContent = "采集中";
        } else if (last) {
          label.textContent = `最近 ${String(last.started_at).slice(5, 16)}`;
        } else {
          label.textContent = "未采集";
        }
      })
      .catch(() => {});
  }

  updateCollectStatus();
  setInterval(updateCollectStatus, 15000);
})();
