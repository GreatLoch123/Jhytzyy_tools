(function () {
  window.NetOps = window.NetOps || {};

  let items = [];
  let search = "";
  let category = "";
  let initialized = false;

  function init() {
    if (initialized) return;
    initialized = true;
    document.getElementById("navSearch").addEventListener("input", (event) => {
      search = event.target.value;
      render();
    });
    document.getElementById("navCategoryFilter").addEventListener("change", (event) => {
      category = event.target.value;
      render();
    });
    document.getElementById("addNavBtn").addEventListener("click", () => openForm());
    document.getElementById("navGroups").addEventListener("click", handleGroupClick);
    load();
  }

  async function load() {
    try {
      const data = await NetOps.api("/api/nav-links");
      items = data.items || [];
      render();
    } catch (err) {
      NetOps.toast(err.message, "error");
    }
  }

  function render() {
    const groupsEl = document.getElementById("navGroups");
    const countEl = document.getElementById("navCount");
    const filterEl = document.getElementById("navCategoryFilter");
    const query = search.trim().toLowerCase();

    const filtered = items.filter((item) => {
      if (!item.enabled) return false;
      if (category && item.category !== category) return false;
      if (!query) return true;
      return `${item.title} ${item.url} ${item.category}`.toLowerCase().includes(query);
    });

    const categories = [...new Set(items.map((item) => item.category).filter(Boolean))].sort(
      (a, b) => a.localeCompare(b, "zh-CN"),
    );
    filterEl.innerHTML = `<option value="">全部分类</option>${categories
      .map((name) => `<option value="${NetOps.esc(name)}">${NetOps.esc(name)}</option>`)
      .join("")}`;
    filterEl.value = category;

    countEl.textContent = `${filtered.length} 个站点`;

    const groups = {};
    filtered.forEach((item) => {
      (groups[item.category] ||= []).push(item);
    });

    const groupEntries = Object.entries(groups).sort(([a], [b]) => a.localeCompare(b, "zh-CN"));
    if (groupEntries.length === 0) {
      groupsEl.innerHTML = '<div class="empty-state">暂无匹配的网址</div>';
      NetOps.refreshIcons();
      return;
    }

    groupsEl.innerHTML = groupEntries
      .map(
        ([name, links]) => `
          <section class="nav-group">
            <div class="nav-group-head">
              <h3>${NetOps.esc(name)}</h3>
              <span>${links.length}</span>
            </div>
            <div class="nav-grid">
              ${links
                .map(
                  (link) => `
                    <div class="nav-card" data-id="${link.id}">
                      <a class="nav-card-main" href="${NetOps.esc(link.url)}" target="_blank" rel="noopener">
                        <span class="nav-icon">${NetOps.esc(link.icon || link.title.slice(0, 1))}</span>
                        <span class="nav-card-body">
                          <strong>${NetOps.esc(link.title)}</strong>
                          <small>${NetOps.esc(link.url.replace(/^https?:\/\//, ""))}</small>
                        </span>
                      </a>
                      <span class="nav-card-actions">
                        <button class="icon-btn sm" type="button" data-action="edit" data-id="${link.id}" title="编辑">
                          <i data-lucide="pencil"></i>
                        </button>
                        <button class="icon-btn sm danger" type="button" data-action="delete" data-id="${link.id}" title="删除">
                          <i data-lucide="trash-2"></i>
                        </button>
                      </span>
                    </div>
                  `,
                )
                .join("")}
            </div>
          </section>
        `,
      )
      .join("");
    NetOps.refreshIcons();
  }

  function handleGroupClick(event) {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    event.preventDefault();
    event.stopPropagation();
    const id = Number(button.dataset.id);
    const item = items.find((entry) => entry.id === id);
    if (!item) return;
    if (button.dataset.action === "edit") openForm(item);
    if (button.dataset.action === "delete") deleteLink(item);
  }

  function openForm(item) {
    const categories = [...new Set(items.map((entry) => entry.category).filter(Boolean))];
    NetOps.modal.open({
      title: item ? "编辑网址" : "添加网址",
      body: `
        <div class="form-grid">
          <label>
            <span>名称</span>
            <input id="fTitle" type="text" value="${NetOps.esc(item?.title || "")}" />
          </label>
          <label>
            <span>地址</span>
            <input id="fUrl" type="url" value="${NetOps.esc(item?.url || "")}" />
          </label>
          <label>
            <span>分类</span>
            <input id="fCategory" type="text" list="navCategoryOptions" value="${NetOps.esc(item?.category || "常用")}" />
            <datalist id="navCategoryOptions">
              ${categories.map((name) => `<option value="${NetOps.esc(name)}"></option>`).join("")}
            </datalist>
          </label>
          <label>
            <span>图标字符</span>
            <input id="fIcon" type="text" maxlength="2" value="${NetOps.esc(item?.icon || "")}" />
          </label>
          <label>
            <span>排序</span>
            <input id="fSort" type="number" value="${item?.sort_order ?? 0}" />
          </label>
          <label class="checkbox-line">
            <input id="fEnabled" type="checkbox" ${item?.enabled === 0 ? "" : "checked"} />
            <span>启用</span>
          </label>
        </div>
      `,
      footer:
        '<button class="ghost-btn" data-action="cancel" type="button">取消</button>' +
        '<button class="primary-btn" data-action="save" type="button"><i data-lucide="check"></i><span>保存</span></button>',
      onMount(dialog) {
        dialog.querySelector('[data-action="cancel"]').addEventListener("click", NetOps.modal.close);
        dialog.querySelector('[data-action="save"]').addEventListener("click", async () => {
          const payload = {
            title: dialog.querySelector("#fTitle").value.trim(),
            url: dialog.querySelector("#fUrl").value.trim(),
            category: dialog.querySelector("#fCategory").value.trim() || "常用",
            icon: dialog.querySelector("#fIcon").value.trim(),
            sort_order: Number(dialog.querySelector("#fSort").value || 0),
            enabled: dialog.querySelector("#fEnabled").checked,
          };
          if (!payload.title || !payload.url) {
            NetOps.toast("名称和地址不能为空", "warn");
            return;
          }
          try {
            if (item) await NetOps.api(`/api/nav-links/${item.id}`, { method: "PUT", body: JSON.stringify(payload) });
            else await NetOps.api("/api/nav-links", { method: "POST", body: JSON.stringify(payload) });
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

  async function deleteLink(item) {
    const ok = await NetOps.modal.confirm(`删除网址「${item.title}」？`, "删除网址");
    if (!ok) return;
    try {
      await NetOps.api(`/api/nav-links/${item.id}`, { method: "DELETE" });
      NetOps.toast("已删除");
      load();
    } catch (err) {
      NetOps.toast(err.message, "error");
    }
  }

  NetOps.initNavs = init;
  NetOps.refreshNavs = load;
})();
