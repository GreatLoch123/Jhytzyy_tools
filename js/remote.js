(function () {
  window.NetOps = window.NetOps || {};

  const COMMON_REMOTE = [
    { port: 3389, service: "RDP 远程桌面" },
    { port: 22, service: "SSH" },
    { port: 23, service: "Telnet" },
    { port: 5900, service: "VNC" },
    {port:3899, service: "Radmin"},
    {port:9182, service: "群思软件"},
    { port: 443, service: "HTTPS 网页管理" },
    { port: 80, service: "HTTP 网页管理" },
  ];

  let targets = [];
  let logs = [];
  let running = false;
  let initialized = false;

  function init() {
    if (initialized) return;
    initialized = true;
    document.getElementById("addRemoteBtn").addEventListener("click", () => openForm());
    document.getElementById("quickTestBtn").addEventListener("click", quickTest);
    document.getElementById("customTestBtn").addEventListener("click", customTest);
    document.getElementById("remoteTargetList").addEventListener("click", handleListClick);
    document.getElementById("portPresets").addEventListener("click", (event) => {
      const chip = event.target.closest("button[data-port]");
      if (!chip) return;
      document.getElementById("testPorts").value = chip.dataset.port;
    });
    loadTargets();
  }

  async function loadTargets() {
    try {
      const data = await NetOps.api("/api/remote-targets");
      targets = data.items || [];
      renderTargets();
    } catch (err) {
      NetOps.toast(err.message, "error");
    }
  }

  function renderTargets() {
    const listEl = document.getElementById("remoteTargetList");
    const countEl = document.getElementById("remoteCount");
    countEl.textContent = `${targets.length} 种远程方式`;
    if (targets.length === 0) {
      listEl.innerHTML = '<div class="empty-state small">暂无远程方式，可点击右上角维护</div>';
      NetOps.refreshIcons();
      return;
    }
    listEl.innerHTML = targets
      .filter((item) => item.enabled)
      .map(
        (item) => `
          <div class="remote-target-card" data-id="${item.id}">
            <div class="remote-target-head">
              <span class="protocol-badge">${NetOps.esc(item.protocol)}</span>
              <strong>${NetOps.esc(item.name)}</strong>
              <span class="status-badge ${item.host ? "ok" : "muted"}">${item.host ? "已配置" : "待配置"}</span>
            </div>
            <div class="remote-target-meta">
              <span class="mono">${NetOps.esc(item.host || "未填写主机")}</span>
              <span>${NetOps.esc(item.default_ports)}</span>
              ${item.category ? `<span class="category-tag">${NetOps.esc(item.category)}</span>` : ""}
            </div>
            <div class="remote-target-actions">
              <button class="icon-btn sm" type="button" data-action="edit" title="编辑">
                <i data-lucide="pencil"></i>
              </button>
              <button class="icon-btn sm danger" type="button" data-action="delete" title="删除">
                <i data-lucide="trash-2"></i>
              </button>
            </div>
          </div>
        `,
      )
      .join("");
    NetOps.refreshIcons();
  }

  function handleListClick(event) {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const card = button.closest(".remote-target-card");
    const item = targets.find((entry) => entry.id === Number(card.dataset.id));
    if (!item) return;
    if (button.dataset.action === "edit") openForm(item);
    if (button.dataset.action === "delete") deleteTarget(item);
  }

  function parsePorts(value) {
    return String(value || "")
      .split(/[,，;\s]+/)
      .map((part) => Number(part))
      .filter((port) => Number.isInteger(port) && port > 0 && port <= 65535);
  }

  function serviceName(port) {
    const match = COMMON_REMOTE.find((item) => item.port === port);
    if (match) return match.service;
    if (port === 445) return "SMB";
    if (port === 21) return "FTP";
    if(port ===3899) return "Radmin"
    return "自定义端口";
  }

  function setTestStatus(text, type = "muted") {
    const badge = document.getElementById("testStatus");
    badge.className = `status-badge ${type}`;
    badge.textContent = text;
  }

  async function runPortTest(host, ports, label) {
    const parsed = [...new Set(ports)];
    if (!host || parsed.length === 0) {
      NetOps.toast("请输入目标地址和端口", "warn");
      return;
    }
    if (running) return;
    running = true;
    setTestStatus("检测中...", "running");
    try {
      const data = await NetOps.api("/api/port-test", {
        method: "POST",
        body: JSON.stringify({ host, ports: parsed, target: label }),
      });
      logs = (data.results || []).map((item) => ({
        ...item,
        service: serviceName(item.port),
        host,
      }));
      renderLogs();
      const openCount = logs.filter((item) => item.result === "open").length;
      setTestStatus(
        `完成 · ${parsed.length} 个端口，${openCount} 个可用`,
        openCount > 0 ? "ok" : "warn",
      );
    } catch (err) {
      setTestStatus("检测失败", "error");
      NetOps.toast(err.message, "error");
    } finally {
      running = false;
    }
  }

  function quickTest() {
    const host = document.getElementById("testHost").value.trim();
    runPortTest(
      host,
      COMMON_REMOTE.map((item) => item.port),
      "常用远程检测",
    );
  }

  function customTest() {
    const host = document.getElementById("testHost").value.trim();
    const ports = parsePorts(document.getElementById("testPorts").value);
    if (!host) {
      NetOps.toast("请输入目标地址", "warn");
      return;
    }
    if (ports.length === 0) {
      NetOps.toast("请输入要测试的端口", "warn");
      return;
    }
    runPortTest(host, ports, "自定义端口检测");
  }

  function renderLogs() {
    const body = document.getElementById("portTestResults");
    const empty = document.getElementById("portTestEmpty");
    if (logs.length === 0) {
      body.innerHTML = "";
      empty.style.display = "block";
      return;
    }
    empty.style.display = "none";
    const classMap = { open: "ok", closed: "muted", timeout: "warn", error: "error" };
    const labelMap = { open: "开放", closed: "关闭", timeout: "超时", error: "错误" };
    body.innerHTML = logs
      .map(
        (log) => `
          <tr>
            <td class="mono">${log.port}</td>
            <td>${NetOps.esc(log.service || "--")}</td>
            <td><span class="status-badge ${classMap[log.result] || "muted"}">${labelMap[log.result] || NetOps.esc(log.result)}</span></td>
            <td class="muted">${NetOps.esc(log.detail || "--")}</td>
          </tr>
        `,
      )
      .join("");
  }

  function openForm(item) {
    NetOps.modal.open({
      title: item ? "编辑远程方式" : "添加远程方式",
      body: `
        <div class="form-grid">
          <label><span>名称</span><input id="rName" type="text" value="${NetOps.esc(item?.name || "")}" /></label>
          <label><span>协议</span>
            <select id="rProtocol">
              ${["RDP", "SSH", "Telnet", "VNC", "Web", "Other"]
                .map((value) => `<option value="${value}" ${item?.protocol === value ? "selected" : ""}>${value}</option>`)
                .join("")}
            </select>
          </label>
          <label><span>主机地址</span><input id="rHost" type="text" value="${NetOps.esc(item?.host || "")}" /></label>
          <label><span>默认端口</span><input id="rPorts" type="text" value="${NetOps.esc(item?.default_ports || "3389")}" /></label>
          <label><span>分类</span><input id="rCategory" type="text" value="${NetOps.esc(item?.category || "")}" /></label>
          <label><span>备注</span><input id="rNote" type="text" value="${NetOps.esc(item?.note || "")}" /></label>
          <label class="checkbox-line"><input id="rEnabled" type="checkbox" ${item?.enabled === 0 ? "" : "checked"} /><span>启用</span></label>
        </div>
      `,
      footer:
        '<button class="ghost-btn" data-action="cancel" type="button">取消</button>' +
        '<button class="primary-btn" data-action="save" type="button"><i data-lucide="check"></i><span>保存</span></button>',
      onMount(dialog) {
        dialog.querySelector('[data-action="cancel"]').addEventListener("click", NetOps.modal.close);
        dialog.querySelector('[data-action="save"]').addEventListener("click", async () => {
          const payload = {
            name: dialog.querySelector("#rName").value.trim(),
            protocol: dialog.querySelector("#rProtocol").value,
            host: dialog.querySelector("#rHost").value.trim(),
            default_ports: dialog.querySelector("#rPorts").value.trim(),
            category: dialog.querySelector("#rCategory").value.trim(),
            note: dialog.querySelector("#rNote").value.trim(),
            enabled: dialog.querySelector("#rEnabled").checked,
          };
          if (!payload.name) {
            NetOps.toast("名称不能为空", "warn");
            return;
          }
          try {
            if (item) {
              await NetOps.api(`/api/remote-targets/${item.id}`, { method: "PUT", body: JSON.stringify(payload) });
            } else {
              await NetOps.api("/api/remote-targets", { method: "POST", body: JSON.stringify(payload) });
            }
            NetOps.modal.close();
            NetOps.toast("已保存");
            loadTargets();
          } catch (err) {
            NetOps.toast(err.message, "error");
          }
        });
      },
    });
  }

  async function deleteTarget(item) {
    const ok = await NetOps.modal.confirm(`删除远程方式「${item.name}」？`, "删除远程方式");
    if (!ok) return;
    try {
      await NetOps.api(`/api/remote-targets/${item.id}`, { method: "DELETE" });
      NetOps.toast("已删除");
      loadTargets();
    } catch (err) {
      NetOps.toast(err.message, "error");
    }
  }

  NetOps.initRemote = init;
  NetOps.refreshRemote = loadTargets;
})();
