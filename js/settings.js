(function () {
  window.NetOps = window.NetOps || {};

  let switches = [];
  let runs = [];
  let statusData = {};
  let snapshot = {};
  let discoverySettings = {};
  let portFilterGroups = [];
  let portFilterQuery = "";
  let initialized = false;

  function init() {
    if (initialized) return;
    initialized = true;
    document.getElementById("syncWorkbookBtn").addEventListener("click", syncWorkbook);
    document.getElementById("collectAllBtn").addEventListener("click", () => triggerCollect("all"));
    document.getElementById("collectArpBtn").addEventListener("click", () => triggerCollect("arp"));
    document.getElementById("collectMacBtn").addEventListener("click", () => triggerCollect("mac"));
    document.getElementById("saveDiscoveryBtn").addEventListener("click", saveDiscoverySettings);
    document.getElementById("addPortFilterBtn").addEventListener("click", () => openPortFilterForm());
    document.getElementById("importPortFiltersBtn").addEventListener("click", openPortFilterImport);
    document.getElementById("exportPortFiltersBtn").addEventListener("click", exportPortFilters);
    document.getElementById("syncAutoFiltersBtn").addEventListener("click", syncAutoFilters);
    document.getElementById("portFilterSearch").addEventListener("input", (event) => {
      portFilterQuery = event.target.value;
      renderPortFilters();
    });
    document.getElementById("switchTable").addEventListener("click", handleSwitchClick);
    document.getElementById("portFilterList").addEventListener("click", handlePortFilterClick);
    load();
  }

  async function load() {
    try {
      const [statusResult, switchResult, runResult, snapshotResult, filterResult, discoveryResult] = await Promise.all([
        NetOps.api("/api/collect/status"),
        NetOps.api("/api/switches"),
        NetOps.api("/api/collect/runs?limit=1"),
        NetOps.api("/api/snapshot/stats"),
        NetOps.api("/api/port-filters"),
        NetOps.api("/api/discovery-settings"),
      ]);
      statusData = statusResult || {};
      switches = switchResult.items || [];
      runs = runResult.items || [];
      snapshot = snapshotResult || {};
      portFilterGroups = filterResult.groups || [];
      discoverySettings = discoveryResult || {};
      render();
    } catch (err) {
      NetOps.toast(err.message, "error");
    }
  }

  function render() {
    const metaEl = document.getElementById("collectMeta");
    const statsEl = document.getElementById("collectStats");
    const summaryEl = document.getElementById("switchSummary");
    const tableBody = document.getElementById("switchTable");
    const emptyEl = document.getElementById("switchEmpty");
    const runsBody = document.getElementById("collectRunsTable");
    const runsEmpty = document.getElementById("runsEmpty");
    const discoveryEnabled = document.getElementById("discoveryEnabled");
    const discoveryInterval = document.getElementById("discoveryInterval");
    const discoveryStatus = document.getElementById("discoveryStatus");
    const discoveryMeta = document.getElementById("discoveryMeta");

    const enabledCount = switches.filter((item) => item.enabled).length;
    metaEl.textContent = `自动发现：${discoverySettings.enabled ? `每 ${discoverySettings.interval_minutes} 分钟` : "已关闭"} · 最近：${NetOps.esc(
      statusData.lastRuns?.[0]?.started_at || "--",
    )}`;
    discoveryEnabled.checked = Boolean(discoverySettings.enabled);
    discoveryInterval.value = discoverySettings.interval_minutes || 60;
    discoveryStatus.textContent = discoverySettings.enabled ? "已启用" : "已关闭";
    discoveryStatus.className = `status-badge ${discoverySettings.enabled ? "ok" : "muted"}`;
    discoveryMeta.textContent = discoverySettings.enabled
      ? `下次：${discoverySettings.next_run_at || "正在安排"} · 上次完成：${discoverySettings.last_finished_at || "--"}`
      : `上次完成：${discoverySettings.last_finished_at || "--"}`;
    statsEl.innerHTML = `
      <div class="stat-card ${statusData.running ? "accent-coral" : ""}">
        <span class="stat-value">${statusData.running ? "采集中" : "空闲"}</span>
        <span class="stat-label">采集状态</span>
      </div>
      <div class="stat-card"><span class="stat-value">${switches.length}</span><span class="stat-label">交换机总数</span></div>
      <div class="stat-card accent-green"><span class="stat-value">${enabledCount}</span><span class="stat-label">已启用</span></div>
      <div class="stat-card accent-cyan"><span class="stat-value">${snapshot.arpRows ?? 0}</span><span class="stat-label">ARP 记录</span></div>
      <div class="stat-card accent-amber"><span class="stat-value">${snapshot.macRows ?? 0}</span><span class="stat-label">MAC 记录</span></div>
      <div class="stat-card"><span class="stat-value small">${discoverySettings.enabled ? `${discoverySettings.interval_minutes} 分钟` : "已关闭"}</span><span class="stat-label">自动发现</span></div>
    `;
    summaryEl.textContent = `${switches.length} 台 · 启用 ${enabledCount} 台`;

    if (switches.length === 0) {
      tableBody.innerHTML = "";
      emptyEl.style.display = "block";
    } else {
      emptyEl.style.display = "none";
      tableBody.innerHTML = switches
        .map(
          (item) => `
            <tr data-id="${item.id}">
              <td>
                <strong>${NetOps.esc(item.name)}</strong>
                <small class="muted">${NetOps.esc(item.uplink_port || item.uplink_ip || "")}</small>
              </td>
              <td class="mono">${NetOps.esc(item.ip)}</td>
              <td>
                <select data-field="role" class="select-input sm">
                  <option value="both" ${item.role === "both" ? "selected" : ""}>全部</option>
                  <option value="core" ${item.role === "core" ? "selected" : ""}>核心</option>
                  <option value="access" ${item.role === "access" ? "selected" : ""}>接入</option>
                </select>
              </td>
              <td><input data-field="snmp" class="text-input sm" value="${NetOps.esc(item.snmp_community || "")}" /></td>
              <td><label class="switch-check"><input type="checkbox" data-field="enabled" ${item.enabled ? "checked" : ""} /></label></td>
              <td>
                <button class="secondary-btn sm" type="button" data-action="save">
                  <i data-lucide="check"></i><span>保存</span>
                </button>
                <button class="secondary-btn sm danger" type="button" data-action="delete">
                  <i data-lucide="trash-2"></i><span>删除</span>
                </button>
              </td>
            </tr>
          `,
        )
        .join("");
    }

    if (runs.length === 0) {
      runsBody.innerHTML = "";
      runsEmpty.style.display = "block";
    } else {
      runsEmpty.style.display = "none";
      const kindLabel = { all: "全部", arp: "ARP", mac: "MAC" };
      const statusLabel = { running: "采集中", ok: "成功", partial: "部分失败" };
      const statusClass = { running: "running", ok: "ok", partial: "warn" };
      runsBody.innerHTML = runs
        .map(
          (run) => `
            <tr>
              <td class="muted">${NetOps.esc(run.started_at || "--")}</td>
              <td>${kindLabel[run.kind] || NetOps.esc(run.kind)}</td>
              <td>${run.switch_count ?? 0}</td>
              <td>${run.success_count ?? 0}</td>
              <td>${run.record_count ?? 0} / ${run.change_count ?? 0}</td>
              <td><span class="status-badge ${statusClass[run.status] || "muted"}">${statusLabel[run.status] || NetOps.esc(run.status)}</span></td>
              <td class="muted fail-cell" title="${NetOps.esc(run.failed || "")}">
                ${run.failed ? NetOps.esc(String(run.failed).split("\n")[0]) : "--"}
              </td>
            </tr>
          `,
        )
        .join("");
    }
    NetOps.refreshIcons();
    renderPortFilters();
  }

  async function saveDiscoverySettings() {
    const interval = Number(document.getElementById("discoveryInterval").value);
    const enabled = document.getElementById("discoveryEnabled").checked;
    if (!Number.isInteger(interval) || interval < 5 || interval > 10080) {
      NetOps.toast("采集间隔需为 5 到 10080 分钟之间的整数", "warn");
      return;
    }
    try {
      const result = await NetOps.api("/api/discovery-settings", {
        method: "POST", body: JSON.stringify({ enabled, interval_minutes: interval }),
      });
      discoverySettings = result || {};
      NetOps.toast(enabled ? `自动发现已启用，每 ${interval} 分钟采集一次` : "自动发现已关闭");
      render();
    } catch (err) {
      NetOps.toast(err.message, "error");
    }
  }

  function renderPortFilters() {
    const listEl = document.getElementById("portFilterList");
    const emptyEl = document.getElementById("portFilterEmpty");
    const summaryEl = document.getElementById("portFilterSummary");
    const query = portFilterQuery.trim().toLowerCase();
    const total = portFilterGroups.reduce((sum, group) => sum + group.port_filters.length, 0);
    summaryEl.textContent = `${total} 条排除规则`;

    const groups = portFilterGroups
      .filter((group) => {
        if (!query) return true;
        return (
          `${group.switch_name} ${group.switch_ip} ${group.port_filters
            .map((item) => item.port_name)
            .join(" ")}`.toLowerCase().includes(query)
        );
      })
      .filter((group) => group.port_filters.length > 0);

    if (groups.length === 0) {
      listEl.innerHTML = "";
      emptyEl.style.display = "block";
      NetOps.refreshIcons();
      return;
    }
    emptyEl.style.display = "none";

    listEl.innerHTML = groups
      .map(
        (group) => `
          <div class="port-filter-group">
            <div class="port-filter-group-head">
              <strong>${NetOps.esc(group.switch_name || group.switch_ip)}</strong>
              <span class="mono">${NetOps.esc(group.switch_ip)}</span>
              <span class="muted">${group.port_filters.length} 个端口</span>
            </div>
            <div class="port-filter-tags">
              ${group.port_filters
                .map(
                  (item) => `
                    <span class="port-filter-tag ${item.auto_generated ? "auto" : ""}">
                      <span class="mono">${NetOps.esc(item.port_name)}</span>
                      <small>${NetOps.esc(item.reason || (item.auto_generated ? "自动排除" : "手动排除"))}</small>
                      <button class="icon-btn sm" type="button" data-action="edit" data-id="${item.id}" title="编辑">
                        <i data-lucide="pencil"></i>
                      </button>
                      <button class="icon-btn sm danger" type="button" data-action="delete" data-id="${item.id}" title="删除">
                        <i data-lucide="x"></i>
                      </button>
                    </span>
                  `,
                )
                .join("")}
            </div>
          </div>
        `,
      )
      .join("");
    NetOps.refreshIcons();
  }

  function handlePortFilterClick(event) {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const id = Number(button.dataset.id);
    let item = null;
    portFilterGroups.forEach((group) => {
      const found = group.port_filters.find((entry) => entry.id === id);
      if (found) item = { ...found, switch_name: group.switch_name };
    });
    if (!item) return;
    if (button.dataset.action === "edit") openPortFilterForm(item);
    if (button.dataset.action === "delete") deletePortFilter(item);
  }

  function openPortFilterForm(item) {
    NetOps.modal.open({
      title: item ? "编辑排除端口" : "添加排除端口",
      body: `
        <div class="form-grid">
          <label><span>交换机</span>
            <select id="pfSwitch">
              ${switches
                .map(
                  (sw) =>
                    `<option value="${NetOps.esc(sw.ip)}" ${item?.switch_ip === sw.ip ? "selected" : ""}>${NetOps.esc(sw.name)} (${NetOps.esc(sw.ip)})</option>`,
                )
                .join("")}
            </select>
          </label>
          <label><span>端口</span><input id="pfPort" type="text" placeholder="例如 GigabitEthernet1/0/10" value="${NetOps.esc(item?.port_name || "")}" /></label>
          <label class="full"><span>原因</span><input id="pfReason" type="text" placeholder="例如 下联交换机口 / 级联口" value="${NetOps.esc(item?.reason || "")}" /></label>
        </div>
      `,
      footer:
        '<button class="ghost-btn" data-action="cancel" type="button">取消</button>' +
        '<button class="primary-btn" data-action="save" type="button"><i data-lucide="check"></i><span>保存</span></button>',
      onMount(dialog) {
        dialog.querySelector('[data-action="cancel"]').addEventListener("click", NetOps.modal.close);
        dialog.querySelector('[data-action="save"]').addEventListener("click", async () => {
          const payload = {
            switch_ip: dialog.querySelector("#pfSwitch").value,
            port_name: dialog.querySelector("#pfPort").value.trim(),
            reason: dialog.querySelector("#pfReason").value.trim(),
          };
          if (!payload.port_name) {
            NetOps.toast("端口不能为空", "warn");
            return;
          }
          try {
            if (item) {
              await NetOps.api(`/api/port-filters/${item.id}`, { method: "PUT", body: JSON.stringify(payload) });
            } else {
              await NetOps.api("/api/port-filters", { method: "POST", body: JSON.stringify(payload) });
            }
            NetOps.modal.close();
            NetOps.toast("已保存");
            load();
          } catch (err) {
            NetOps.toast(err.message, "error");
          }
        });
      },
    });
  }

  async function deletePortFilter(item) {
    const ok = await NetOps.modal.confirm(`删除端口 ${item.port_name} 的排除规则？`, "删除排除规则");
    if (!ok) return;
    try {
      await NetOps.api(`/api/port-filters/${item.id}`, { method: "DELETE" });
      NetOps.toast("已删除");
      load();
    } catch (err) {
      NetOps.toast(err.message, "error");
    }
  }

  async function syncAutoFilters() {
    try {
      const result = await NetOps.api("/api/port-filters/sync-auto", { method: "POST", body: "{}" });
      NetOps.toast(`自动补全完成：新增 ${result.added ?? 0} 条`);
      load();
    } catch (err) {
      NetOps.toast(err.message, "error");
    }
  }

  async function exportPortFilters() {
    try {
      const data = await NetOps.api("/api/port-filters/export");
      const rows = data.items || [];
      NetOps.downloadCsv(
        `端口排除清单_${new Date().toISOString().slice(0, 10)}.csv`,
        ["交换机IP", "端口", "原因", "自动生成", "更新时间"],
        rows.map((item) => [item.switch_ip, item.port_name, item.reason, item.auto_generated ? "是" : "否", item.updated_at]),
      );
      NetOps.toast(`已导出 ${rows.length} 条规则`);
    } catch (err) {
      NetOps.toast(err.message, "error");
    }
  }

  function openPortFilterImport() {
    NetOps.modal.open({
      title: "导入端口排除清单",
      body: `
        <div class="form-grid single">
          <label><span>文件</span><input id="pfImportFile" type="file" accept=".csv,.txt,.xlsx" /></label>
          <p class="panel-hint">表头需包含“交换机IP”和“端口”；“原因”为可选列。可先导出清单后编辑再导入。</p>
        </div>
      `,
      footer:
        '<button class="ghost-btn" data-action="cancel" type="button">取消</button>' +
        '<button class="primary-btn" data-action="import" type="button"><i data-lucide="upload"></i><span>导入</span></button>',
      onMount(dialog) {
        dialog.querySelector('[data-action="cancel"]').addEventListener("click", NetOps.modal.close);
        dialog.querySelector('[data-action="import"]').addEventListener("click", async () => {
          const file = dialog.querySelector("#pfImportFile").files?.[0];
          if (!file) {
            NetOps.toast("请选择导入文件", "warn");
            return;
          }
          try {
            const result = await NetOps.api("/api/port-filters/import", {
              method: "POST", raw: true, headers: { "Content-Type": "application/octet-stream", "X-Filename": file.name }, body: file,
            });
            NetOps.modal.close();
            NetOps.toast(`导入完成：新增 ${result.imported ?? 0}，更新 ${result.updated ?? 0}，跳过 ${result.skipped ?? 0}`);
            load();
          } catch (err) {
            NetOps.toast(err.message, "error");
          }
        });
      },
    });
  }

  function handleSwitchClick(event) {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const row = button.closest("tr");
    const id = Number(row.dataset.id);
    if (button.dataset.action === "delete") {
      deleteSwitch(id, row.querySelector("strong")?.textContent || "该交换机");
      return;
    }
    if (button.dataset.action !== "save") return;
    const payload = {
      role: row.querySelector('[data-field="role"]').value,
      snmp_community: row.querySelector('[data-field="snmp"]').value.trim(),
      enabled: row.querySelector('[data-field="enabled"]').checked,
    };
    NetOps.api(`/api/switches/${id}`, { method: "PUT", body: JSON.stringify(payload) })
      .then(() => {
        NetOps.toast("已保存");
        load();
      })
      .catch((err) => NetOps.toast(err.message, "error"));
  }

  async function deleteSwitch(id, name) {
    const ok = await NetOps.modal.confirm(`删除采集清单中的“${name}”？对应端口排除规则也会删除，历史采集数据保留。`, "删除采集对象");
    if (!ok) return;
    try {
      await NetOps.api(`/api/switches/${id}`, { method: "DELETE" });
      NetOps.toast("已删除采集对象");
      load();
    } catch (err) {
      NetOps.toast(err.message, "error");
    }
  }

  async function syncWorkbook() {
    try {
      const result = await NetOps.api("/api/switches/sync", { method: "POST", body: "{}" });
      NetOps.toast(`已同步：新增 ${result.added ?? 0}，更新 ${result.updated ?? 0}`);
      load();
    } catch (err) {
      NetOps.toast(err.message, "error");
    }
  }

  function triggerCollect(kind) {
    NetOps.api(`/api/collect/now?kind=${kind}`, { method: "POST", body: "{}" })
      .then(() => {
        NetOps.toast("采集任务已启动", "warn");
        pollRunning();
      })
      .catch((err) => NetOps.toast(err.message, "error"));
  }

  async function pollRunning() {
    for (let attempt = 0; attempt < 60; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 3000));
      try {
        const status = await NetOps.api("/api/collect/status");
        if (!status.running) {
          load();
          return;
        }
      } catch (err) {
        return;
      }
    }
    load();
  }

  NetOps.initSettings = init;
  NetOps.refreshSettings = load;
})();
