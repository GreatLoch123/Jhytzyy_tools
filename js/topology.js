(function () {
  window.NetOps = window.NetOps || {};

  const topologyData = {
    switches: [
      { id: "core-a", name: "Core-A", ip: "10.10.0.1", x: 600, y: 110 },
      { id: "agg-a", name: "Agg-A", ip: "10.10.1.1", x: 330, y: 295 },
      { id: "agg-b", name: "Agg-B", ip: "10.10.2.1", x: 870, y: 295 },
      { id: "edge-a", name: "Edge-A", ip: "10.10.11.1", x: 180, y: 560 },
      { id: "edge-b", name: "Edge-B", ip: "10.10.12.1", x: 470, y: 590 },
      { id: "edge-c", name: "Edge-C", ip: "10.10.21.1", x: 730, y: 590 },
      { id: "edge-d", name: "Edge-D", ip: "10.10.22.1", x: 1020, y: 560 },
    ],
    links: [
      { id: "l1", source: "core-a", target: "agg-a", status: "up", bandwidth: "40G" },
      { id: "l2", source: "core-a", target: "agg-b", status: "up", bandwidth: "40G" },
      { id: "l3", source: "agg-a", target: "agg-b", status: "up", bandwidth: "20G" },
      { id: "l4", source: "agg-a", target: "edge-a", status: "up", bandwidth: "10G" },
      { id: "l5", source: "agg-a", target: "edge-b", status: "down", bandwidth: "10G" },
      { id: "l6", source: "agg-b", target: "edge-c", status: "up", bandwidth: "10G" },
      { id: "l7", source: "agg-b", target: "edge-d", status: "up", bandwidth: "10G" },
    ],
  };

  let initialized = false;

  function createSvgElement(tag, attributes = {}) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, value));
    return element;
  }

  function nodeById(data) {
    return new Map(data.switches.map((item) => [item.id, item]));
  }

  function hasDownLink(data, switchId) {
    return data.links.some(
      (link) => link.status === "down" && (link.source === switchId || link.target === switchId),
    );
  }

  function curvedPath(source, target) {
    const midX = (source.x + target.x) / 2;
    const midY = (source.y + target.y) / 2;
    const dx = target.x - source.x;
    const dy = target.y - source.y;
    const normal = Math.max(Math.min(Math.hypot(dx, dy) * 0.12, 62), 24);
    const controlX = midX - (dy / Math.max(Math.hypot(dx, dy), 1)) * normal;
    const controlY = midY + (dx / Math.max(Math.hypot(dx, dy), 1)) * normal;

    return `M ${source.x} ${source.y} Q ${controlX} ${controlY} ${target.x} ${target.y}`;
  }

  function init() {
    if (initialized) return;
    initialized = true;

    const svg = document.querySelector("#topologySvg");
    const linksLayer = document.querySelector("#linksLayer");
    const nodesLayer = document.querySelector("#nodesLayer");
    const linkList = document.querySelector("#linkList");
    const switchCount = document.querySelector("#switchCount");
    const linkCount = document.querySelector("#linkCount");
    const downCount = document.querySelector("#downCount");
    const simulateBtn = document.querySelector("#simulateBtn");
    const zabbixStatus = document.querySelector("#zabbixStatus");
    const sideToggle = document.querySelector("#sideToggle");
    const sidePanel = document.querySelector("#sidePanel");
    const topologyMeta = document.querySelector("#topologyMeta");
    const reloadBtn = document.querySelector("#reloadTopologyBtn");

    let currentData = structuredClone(topologyData);
    let usingApiData = false;

    function renderLinks(data) {
      const nodes = nodeById(data);
      linksLayer.replaceChildren();

      data.links.forEach((link) => {
        const source = nodes.get(link.source);
        const target = nodes.get(link.target);
        if (!source || !target) return;

        const pathId = `path-${link.id}`;
        const path = createSvgElement("path", {
          id: pathId,
          class: `backbone ${link.status === "down" ? "down" : ""}`,
          d: curvedPath(source, target),
        });

        const packet = createSvgElement("circle", {
          r: link.status === "down" ? 7 : 5,
          class: `packet ${link.status === "down" ? "down" : ""}`,
        });
        const motion = createSvgElement("animateMotion", {
          dur: link.status === "down" ? "1.6s" : "3.2s",
          repeatCount: "indefinite",
          path: curvedPath(source, target),
        });

        packet.appendChild(motion);
        linksLayer.append(path, packet);
      });
    }

    function renderNodes(data) {
      nodesLayer.replaceChildren();

      data.switches.forEach((item) => {
        const isDown = hasDownLink(data, item.id);
        const group = createSvgElement("g", {
          class: `switch-card ${isDown ? "down" : ""}`,
          transform: `translate(${item.x - 90} ${item.y - 55})`,
        });

        const body = createSvgElement("rect", {
          class: "switch-body",
          width: 180,
          height: 110,
          rx: 8,
        });
        const screen = createSvgElement("rect", {
          class: "switch-screen",
          x: 15,
          y: 12,
          width: 150,
          height: 50,
          rx: 4,
        });
        const label = createSvgElement("text", {
          class: "switch-label",
          x: 90,
          y: 36,
          "text-anchor": "middle",
        });
        const ip = createSvgElement("text", {
          class: "switch-ip",
          x: 90,
          y: 52,
          "text-anchor": "middle",
        });

        label.textContent = item.name;
        ip.textContent = item.ip;
        group.append(body, screen, label, ip);

        for (let index = 0; index < 8; index += 1) {
          group.appendChild(
            createSvgElement("rect", {
              class: `port ${isDown && index < 2 ? "down" : "active"}`,
              x: 26 + index * 17,
              y: 96,
              width: 8,
              height: 4,
              rx: 1,
            }),
          );
        }

        nodesLayer.appendChild(group);
      });
    }

    function renderPanel(data) {
      const nodes = nodeById(data);
      const downLinks = data.links.filter((link) => link.status === "down").length;

      switchCount.textContent = data.switches.length;
      linkCount.textContent = data.links.length;
      downCount.textContent = downLinks;

      linkList.replaceChildren(
        ...data.links.map((link) => {
          const item = document.createElement("article");
          item.className = `link-item ${link.status === "down" ? "down" : ""}`;
          item.innerHTML = `
            <span class="link-dot" aria-hidden="true"></span>
            <span>${NetOps.esc(nodes.get(link.source)?.name ?? link.source)} -> ${NetOps.esc(
              nodes.get(link.target)?.name ?? link.target,
            )}</span>
            <span class="link-status">${link.status === "down" ? "断开" : NetOps.esc(link.bandwidth)}</span>
          `;
          return item;
        }),
      );
    }

    function render(data) {
      currentData = structuredClone(data);
      if (currentData.canvas?.width && currentData.canvas?.height) {
        svg.setAttribute("viewBox", `0 0 ${currentData.canvas.width} ${currentData.canvas.height}`);
      }
      renderLinks(currentData);
      renderNodes(currentData);
      renderPanel(currentData);
    }

    function updateTopology(nextData) {
      render({
        switches: nextData.switches ?? currentData.switches,
        links: nextData.links ?? currentData.links,
      });
    }

    function simulateDisconnect() {
      const next = structuredClone(currentData);
      const candidates = next.links.filter((link) => link.status === "up");
      if (candidates.length === 0) {
        next.links.forEach((link) => {
          link.status = "up";
        });
      } else {
        candidates[Math.floor(Math.random() * candidates.length)].status = "down";
      }
      render(next);
    }

    async function loadRemoteTopology() {
      try {
        const testResp = await fetch("/api/zabbix/test", { cache: "no-store" });
        if (testResp.ok) {
          const testData = await testResp.json();
          if (testData.enabled === false && testData.reason) {
            zabbixStatus.className = "zabbix-status status-warn";
            zabbixStatus.textContent = "Zabbix 未启用：" + testData.reason;
          } else if (testData.versionError) {
            zabbixStatus.className = "zabbix-status status-error";
            zabbixStatus.textContent = "Zabbix 连接失败：" + testData.versionError;
          } else if (testData.matchedHostCount === 0) {
            zabbixStatus.className = "zabbix-status status-warn";
            zabbixStatus.textContent =
              "Zabbix 匹配率 0/" + testData.matchingRate?.split("/")[1] + "，未找到对应主机";
            if (testData.hint) zabbixStatus.title = testData.hint;
          } else if (testData.hostSearchError) {
            zabbixStatus.className = "zabbix-status status-error";
            zabbixStatus.textContent = "主机搜索失败：" + testData.hostSearchError;
          } else {
            zabbixStatus.className = "zabbix-status status-ok";
            zabbixStatus.textContent = "Zabbix 已连接（匹配 " + (testData.matchingRate || "?") + "）";
          }
        }

        const response = await fetch("/api/topology", { cache: "no-store" });
        if (!response.ok) {
          const errBody = await response.json().catch(() => ({}));
          console.warn("拓扑数据 API 错误:", response.status, errBody);
          return;
        }

        const data = await response.json();
        if (Array.isArray(data.switches) && Array.isArray(data.links)) {
          if (data.meta?.zabbix?.error) {
            console.warn("Zabbix 数据获取失败:", data.meta.zabbix.error);
            zabbixStatus.className = "zabbix-status status-error";
            zabbixStatus.textContent = "Zabbix 异常: " + data.meta.zabbix.error.slice(0, 60);
          } else if (zabbixStatus && !zabbixStatus.className.includes("error") && data.meta?.zabbix?.enabled) {
            zabbixStatus.className = "zabbix-status status-ok";
            zabbixStatus.textContent =
              "Zabbix 已同步（匹配 " + (data.meta.zabbix.matchedHosts || "0") + " 台主机）";
          }
          topologyMeta.textContent = `数据源：${data.meta?.collector || "--"} · ${data.switches.length} 台交换机`;
          usingApiData = true;
          render(data);
        } else {
          console.warn("拓扑数据格式异常:", data);
        }
      } catch (err) {
        console.warn("拓扑数据获取失败:", err.message);
        zabbixStatus.className = "zabbix-status status-error";
        zabbixStatus.textContent = "拓扑数据获取失败";
        usingApiData = false;
      }
    }

    simulateBtn.addEventListener("click", simulateDisconnect);
    reloadBtn.addEventListener("click", loadRemoteTopology);
    sideToggle.addEventListener("click", () => {
      const isOpen = sidePanel.classList.toggle("open");
      sideToggle.textContent = isOpen ? "◀" : "▶";
    });

    render(currentData);
    loadRemoteTopology();
    setInterval(() => {
      if (usingApiData) loadRemoteTopology();
    }, 60000);
  }

  NetOps.initTopology = init;
})();
