(function () {
  window.NetOps = window.NetOps || {};

  async function api(path, options = {}) {
    const isForm = options.body instanceof FormData;
    const isRaw = options.raw === true;
    const headers = { ...(options.headers || {}) };
    if (!isForm && !isRaw && options.body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    const { raw, ...rest } = options;
    const response = await fetch(path, { ...rest, headers });
    let data = null;
    try {
      data = await response.json();
    } catch (err) {
      data = null;
    }
    if (!response.ok) {
      const message = (data && (data.error || data.message)) || `请求失败 (${response.status})`;
      throw new Error(message);
    }
    return data;
  }

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => {
      const map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
      return map[char];
    });
  }

  function refreshIcons() {
    if (window.lucide && typeof lucide.createIcons === "function") {
      lucide.createIcons();
    }
  }

  function debounce(fn, delay = 300) {
    let timer = null;
    return (...args) => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => fn(...args), delay);
    };
  }

  function downloadCsv(filename, headers, rows) {
    const quote = (value) => `"${String(value ?? "").replaceAll('"', '""')}"`;
    const csv = [headers.map(quote).join(","), ...rows.map((row) => row.map(quote).join(","))].join("\r\n");
    const url = URL.createObjectURL(new Blob([`\uFEFF${csv}`], { type: "text/csv;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
  }

  NetOps.api = api;
  NetOps.esc = esc;
  NetOps.refreshIcons = refreshIcons;
  NetOps.debounce = debounce;
  NetOps.downloadCsv = downloadCsv;
})();
