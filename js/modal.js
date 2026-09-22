(function () {
  window.NetOps = window.NetOps || {};

  function open({ title, body = "", footer = "", className = "", onMount }) {
    close();
    const root = document.getElementById("modalRoot");
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";

    const dialog = document.createElement("div");
    dialog.className = `modal ${className}`.trim();
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");

    const header = document.createElement("div");
    header.className = "modal-header";
    header.innerHTML = `<h3>${NetOps.esc(title)}</h3>`;
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "icon-btn";
    closeBtn.title = "关闭";
    closeBtn.innerHTML = '<i data-lucide="x"></i>';
    closeBtn.addEventListener("click", close);
    header.appendChild(closeBtn);

    const bodyEl = document.createElement("div");
    bodyEl.className = "modal-body";
    if (typeof body === "string") bodyEl.innerHTML = body;
    else if (body instanceof Node) bodyEl.appendChild(body);

    dialog.append(header, bodyEl);
    if (footer) {
      const footerEl = document.createElement("div");
      footerEl.className = "modal-footer";
      footerEl.innerHTML = footer;
      dialog.appendChild(footerEl);
    }

    overlay.addEventListener("mousedown", (event) => {
      if (event.target === overlay) close();
    });
    overlay.appendChild(dialog);
    root.replaceChildren(overlay);
    document.body.classList.add("modal-open");

    if (typeof onMount === "function") onMount(dialog, overlay);
    NetOps.refreshIcons();
  }

  function close() {
    const root = document.getElementById("modalRoot");
    root.replaceChildren();
    document.body.classList.remove("modal-open");
  }

  function confirm(message, title = "确认操作") {
    return new Promise((resolve) => {
      open({
        title,
        body: `<p class="confirm-text">${NetOps.esc(message)}</p>`,
        className: "modal-sm",
        footer:
          '<button class="ghost-btn" data-action="cancel" type="button">取消</button>' +
          '<button class="danger-btn" data-action="ok" type="button">确认</button>',
        onMount(dialog) {
          dialog.querySelector('[data-action="cancel"]').addEventListener("click", () => {
            close();
            resolve(false);
          });
          dialog.querySelector('[data-action="ok"]').addEventListener("click", () => {
            close();
            resolve(true);
          });
        },
      });
    });
  }

  function toast(message, type = "success") {
    const root = document.getElementById("toastRoot");
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    const iconMap = { success: "check-circle-2", error: "alert-circle", warn: "alert-triangle" };
    el.innerHTML = `<i data-lucide="${iconMap[type] || "info"}"></i><span>${NetOps.esc(message)}</span>`;
    root.appendChild(el);
    NetOps.refreshIcons();
    requestAnimationFrame(() => el.classList.add("show"));
    window.setTimeout(() => {
      el.classList.remove("show");
      window.setTimeout(() => el.remove(), 260);
    }, 3200);
  }

  NetOps.modal = { open, close, confirm };
  NetOps.toast = toast;
})();
