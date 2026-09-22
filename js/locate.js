(function () {
  window.NetOps = window.NetOps || {};

  let items = [];
  let total = 0;
  let stats = {};
  let search = "";
  let filter = "all";
  let mode = localStorage.getItem("netops.locateMode") || "inventory";
  let initialized = false;

  function init() {
    if (initialized) return;
    initialized = true;
    document
      .getElementById("locateSearch")
      .addEventListener("input", NetOps.debounce((event) => {
        search = event.target.value;
        load();
      }, 300));
    document.getElementById("locateFilter").addEventListener("change", (event) => {
      filter = event.target.value;
      load();
    });
    const inventoryMode = document.getElementById("inventoryMode");
    inventoryMode.checked = mode === "inventory";
    inventoryMode.addEventListener("change", (event) => {
      mode = event.target.checked ? "inventory" : "current";
      localStorage.setItem("netops.locateMode", mode);
      load();
    });
    document.getElementById("addUserBtn").addEventListener("click", () => openUserForm());
    document.getElementById("importUsersBtn").addEventListener("click", openImport);
    document.getElementById("exportLocateBtn").addEventListener("click", exportLocate);
    document.getElementById("locateTable").addEventListener("click", handleTableClick);
    load();
  }

  async function exportLocate() {
    try {
      const data = await NetOps.api(`/api/locate/export?mode=${encodeURIComponent(mode)}`);
      const rows = data.items || [];
      NetOps.downloadCsv(
        `IP定位与终端台账_${new Date().toISOString().slice(0, 10)}.csv`,
        ["IP地址", "MAC地址", "使用人", "部门", "位置", "设备类型", "接入交换机", "交换机IP", "端口", "VLAN", "首次发现", "最后发现", "状态", "备注"],
        rows.map((item) => [
          item.ip, item.mac, item.user_name, item.department, item.location, item.device_type,
          item.switchName, item.switchIp, item.portName, item.vlan, item.firstSeen, item.lastSeen || item.arpCollectedAt,
          item.matched ? "当前在线" : "累计收录", item.note,
        ]),
      );
      NetOps.toast(`已导出 ${rows.length} 条记录`);
    } catch (err) {
      NetOps.toast(err.message, "error");
    }
  }

  async function load() {
    try {
      const params = new URLSearchParams({ q: search, filter, mode, limit: "500" });
      const [data, statData] = await Promise.all([
        NetOps.api(`/api/locate?${params.toString()}`),
        NetOps.api("/api/locate/stats"),
      ]);
      items = data.items || [];
      total = data.total || 0;
      stats = statData || {};
      render();
    } catch (err) {
      NetOps.toast(err.message, "error");
    }
  }

  function render() {
    const tableBody = document.getElementById("locateTable");
    const emptyEl = document.getElementById("locateEmpty");
    const countEl = document.getElementById("locateCount");
    const statsEl = document.getElementById("locateStats");

    countEl.textContent = `${total} 条记录`;
    statsEl.innerHTML = `
      <div class="stat-card"><span class="stat-value">${stats.inventoryCount ?? 0}</span><span class="stat-label">累计终端</span></div>
      <div class="stat-card accent-cyan"><span class="stat-value">${stats.online ?? 0}</span><span class="stat-label">最近在线</span></div>
      <div class="stat-card accent-green"><span class="stat-value">${stats.registered ?? 0}</span><span class="stat-label">已登记使用人</span></div>
      <div class="stat-card accent-amber"><span class="stat-value">${stats.unregistered ?? 0}</span><span class="stat-label">未登记使用人</span></div>
      <div class="stat-card accent-coral"><span class="stat-value">${stats.macCount ?? 0}</span><span class="stat-label">快照 MAC</span></div>
      <div class="stat-card"><span class="stat-value small">${NetOps.esc(stats.lastCollection || "--")}</span><span class="stat-label">最近采集</span></div>
    `;

    if (items.length === 0) {
      tableBody.innerHTML = "";
      emptyEl.style.display = "block";
      NetOps.refreshIcons();
      return;
    }
    emptyEl.style.display = "none";
    tableBody.innerHTML = items
      .map(
        (item) => `
          <tr>
            <td class="mono">${NetOps.esc(item.ip)}</td>
            <td class="mono">${NetOps.esc(item.mac || "--")}</td>
            <td>${NetOps.esc(item.user_name || "--")}</td>
            <td>${NetOps.esc(item.department || "--")}</td>
            <td>${NetOps.esc(item.switchName || "--")}</td>
            <td class="mono">${NetOps.esc(item.portName || "--")}</td>
            <td>${item.vlan ?? "--"}</td>
            <td class="muted">${NetOps.esc(item.lastSeen || item.arpCollectedAt || "--")}</td>
            <td><span class="status-badge ${item.matched ? "ok" : "muted"}">${item.matched ? "在线" : "累计"}</span></td>
            <td class="row-actions">
              <button class="icon-btn sm" type="button" data-action="history" title="历史">
                <i data-lucide="history"></i>
              </button>
              <button class="icon-btn sm" type="button" data-action="edit" title="编辑">
                <i data-lucide="pencil"></i>
              </button>
              ${item.user_id ? '<button class="icon-btn sm danger" type="button" data-action="delete" title="删除"><i data-lucide="trash-2"></i></button>' : ""}
            </td>
          </tr>
        `,
      )
      .join("");
    NetOps.refreshIcons();
  }

  function handleTableClick(event) {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const row = button.closest("tr");
    const index = [...row.parentElement.children].indexOf(row);
    const item = items[index];
    if (!item) return;
    const action = button.dataset.action;
    if (action === "history") openHistory(item);
    if (action === "edit") openUserForm(item);
    if (action === "delete") deleteUser(item);
  }

  function openUserForm(item) {
    const isEdit = Boolean(item?.user_id);
    NetOps.modal.open({
      title: isEdit ? "编辑终端记录" : "新增终端记录",
      body: `
        <div class="form-grid">
          <label><span>IP 地址</span><input id="uIp" type="text" value="${NetOps.esc(item?.ip || "")}" ${isEdit ? "readonly" : ""} /></label>
          <label><span>使用人</span><input id="uUser" type="text" value="${NetOps.esc(item?.user_name || "")}" /></label>
          <label><span>部门</span><input id="uDept" type="text" value="${NetOps.esc(item?.department || "")}" /></label>
          <label><span>位置</span><input id="uLoc" type="text" value="${NetOps.esc(item?.location || "")}" /></label>
          <label><span>设备类型</span><input id="uDevice" type="text" value="${NetOps.esc(item?.device_type || "")}" /></label>
          <label><span>备注</span><input id="uNote" type="text" value="${NetOps.esc(item?.note || "")}" /></label>
        </div>
      `,
      footer:
        '<button class="ghost-btn" data-action="cancel" type="button">取消</button>' +
        '<button class="primary-btn" data-action="save" type="button"><i data-lucide="check"></i><span>保存</span></button>',
      onMount(dialog) {
        dialog.querySelector('[data-action="cancel"]').addEventListener("click", NetOps.modal.close);
        dialog.querySelector('[data-action="save"]').addEventListener("click", async () => {
          const payload = {
            ip: dialog.querySelector("#uIp").value.trim(),
            user_name: dialog.querySelector("#uUser").value.trim(),
            department: dialog.querySelector("#uDept").value.trim(),
            location: dialog.querySelector("#uLoc").value.trim(),
            device_type: dialog.querySelector("#uDevice").value.trim(),
            note: dialog.querySelector("#uNote").value.trim(),
          };
          if (!payload.ip) {
            NetOps.toast("IP 不能为空", "warn");
            return;
          }
          try {
            if (isEdit) {
              await NetOps.api(`/api/ip-users/${item.user_id}`, { method: "PUT", body: JSON.stringify(payload) });
            } else {
              await NetOps.api("/api/ip-users", { method: "POST", body: JSON.stringify(payload) });
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

  function openImport() {
    NetOps.modal.open({
      title: "导入 IP 使用人台账",
      body: `
        <div class="form-grid single">
          <label>
            <span>文件</span>
            <input id="importFile" type="file" accept=".csv,.txt,.xlsx" />
          </label>
          <label>
            <span>或粘贴 CSV 文本</span>
            <textarea id="importText" rows="8" placeholder="IP,使用人,部门,位置,设备类型,备注"></textarea>
          </label>
        </div>
      `,
      footer:
        '<button class="ghost-btn" data-action="cancel" type="button">取消</button>' +
        '<button class="primary-btn" data-action="import" type="button"><i data-lucide="upload"></i><span>导入</span></button>',
      onMount(dialog) {
        dialog.querySelector('[data-action="cancel"]').addEventListener("click", NetOps.modal.close);
        dialog.querySelector('[data-action="import"]').addEventListener("click", async () => {
          const fileInput = dialog.querySelector("#importFile");
          const text = dialog.querySelector("#importText").value.trim();
          try {
            let result;
            if (fileInput.files && fileInput.files[0]) {
              const file = fileInput.files[0];
              result = await NetOps.api("/api/ip-users/import", {
                method: "POST",
                raw: true,
                headers: { "Content-Type": "application/octet-stream", "X-Filename": file.name },
                body: file,
              });
            } else if (text) {
              result = await NetOps.api("/api/ip-users/import", {
                method: "POST",
                body: JSON.stringify({ csv: text }),
              });
            } else {
              NetOps.toast("请选择文件或输入 CSV 文本", "warn");
              return;
            }
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

  async function openHistory(item) {
    try {
      const [arpData, macData] = await Promise.all([
        NetOps.api(`/api/arp-history?ip=${encodeURIComponent(item.ip)}`),
        item.mac ? NetOps.api(`/api/mac-history?mac=${encodeURIComponent(item.mac)}`) : Promise.resolve({ items: [] }),
      ]);
      const arpRows = arpData.items || [];
      const macRows = macData.items || [];
      const table = (rows, columns) => {
        if (!rows.length) return '<div class="empty-state small">暂无历史</div>';
        return `
          <table class="data-table compact">
            <thead><tr>${columns.map((col) => `<th>${col.label}</th>`).join("")}</tr></thead>
            <tbody>
              ${rows
                .map(
                  (row) =>
                    `<tr>${columns.map((col) => `<td>${NetOps.esc(row[col.key] ?? "--")}</td>`).join("")}</tr>`,
                )
                .join("")}
            </tbody>
          </table>
        `;
      };
      NetOps.modal.open({
        title: `${item.ip} · 历史记录`,
        className: "modal-wide",
        body: `
          <div class="history-block">
            <h4>ARP 采集历史</h4>
            ${table(arpRows, [
              { key: "collected_at", label: "采集时间" },
              { key: "switch_name", label: "交换机" },
              { key: "switch_ip", label: "交换机 IP" },
              { key: "if_name", label: "接口" },
              { key: "mac", label: "MAC" },
            ])}
          </div>
          <div class="history-block">
            <h4>MAC 接入位置历史</h4>
            ${table(macRows, [
              { key: "collected_at", label: "采集时间" },
              { key: "switch_name", label: "交换机" },
              { key: "switch_ip", label: "交换机 IP" },
              { key: "port_name", label: "端口" },
              { key: "vlan", label: "VLAN" },
              { key: "entry_type", label: "类型" },
            ])}
          </div>
        `,
      });
    } catch (err) {
      NetOps.toast(err.message, "error");
    }
  }

  async function deleteUser(item) {
    if (!item.user_id) return;
    const ok = await NetOps.modal.confirm(`删除 IP ${item.ip} 的使用人记录？`, "删除记录");
    if (!ok) return;
    try {
      await NetOps.api(`/api/ip-users/${item.user_id}`, { method: "DELETE" });
      NetOps.toast("已删除");
      load();
    } catch (err) {
      NetOps.toast(err.message, "error");
    }
  }

  NetOps.initLocate = init;
  NetOps.refreshLocate = load;
})();
