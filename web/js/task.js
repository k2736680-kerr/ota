const STEPS = ["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"];
let currentId = "";
let logOffset = 0;
let timer = null;
let refreshSeq = 0;
let toastTimer = null;

let caseCatalog = [];
let linkCatalog = [];
let firmwareTargets = [];

async function boot() {
  const catalog = await api("/api/config/apps");
  caseCatalog = catalog.cases || [];
  linkCatalog = catalog.links || [];
  document.querySelector("#notice").textContent = catalog.notice || "";
  const link = document.querySelector("#link");
  link.innerHTML = linkCatalog.map((item) => `<option value="${item.id}">${item.name}</option>`).join("");
  updateLinkChip();
  renderFirmwareTargets();
  renderCases();
  const defaults = catalog.defaults || {};
  document.querySelector("#drop").value = defaults.drop_window || 120;
  document.querySelector("#timeout").value = defaults.reconnect_timeout || 180;
  bindActions();
  await refreshDevice({ reason: "启动读取" });
  await loadReports();
  await showDirs();
  renderSteps("");
}

function bindActions() {
  onClick("#start", startTask);
  onClick("#check-all", () => {
    setChecked(true);
    toast("已全选用例");
  });
  onClick("#check-none", () => {
    setChecked(false);
    toast("已取消全选");
  });
  onClick("#run-all", async (button) => {
    document.querySelectorAll("#cases .case-row").forEach((row) => {
      row.querySelector("input[type='checkbox']").checked = row.dataset.strategy !== "s2";
    });
    await withBusy(button, startTask, "启动中…");
  });
  onClick("#stop", stopTask);
  onClick("#resume", resumeTask);
  onClick("#refresh", (button) => withBusy(button, () => refreshDevice({ reason: "手动刷新", force: true }), "刷新中…"));
  onClick("#switch-check", switchCheck);
  onClick("#cleanup", cleanup);
  document.querySelector("#link").addEventListener("change", onLinkChange);
}

function onClick(selector, handler) {
  document.querySelector(selector).addEventListener("click", async (event) => {
    const button = event.currentTarget;
    if (button.disabled) return;
    try {
      await handler(button);
    } catch (error) {
      toast(error.message || String(error), "bad");
      setVerdict(error.message || String(error), true);
    }
  });
}

async function onLinkChange() {
  updateLinkChip();
  renderCases();
  renderFirmwareTargets();
  pulseTaskPanel();
  toast(`已切换到 ${currentLinkName()}，正在读取设备…`);
  await refreshDevice({ reason: "切换 App", clearFirst: true });
}

function currentLinkName() {
  const item = linkCatalog.find((entry) => entry.id === document.querySelector("#link").value);
  return (item && item.name) || document.querySelector("#link").value || "—";
}

function updateLinkChip() {
  const chip = document.querySelector("#link-chip");
  if (chip) chip.textContent = currentLinkName();
}

function pulseTaskPanel() {
  const panel = document.querySelector("#task-panel");
  if (!panel) return;
  panel.classList.remove("pulse");
  void panel.offsetWidth;
  panel.classList.add("pulse");
  setTimeout(() => panel.classList.remove("pulse"), 700);
}

async function refreshDevice(options = {}) {
  const seq = ++refreshSeq;
  const link = document.querySelector("#link").value;
  const pill = document.querySelector("#device");
  const identity = document.querySelector("#identity");
  const status = document.querySelector("#identity-status");
  setIdentityStatus("读取中…", "busy");
  pill.classList.add("busy");
  pill.textContent = `${currentLinkName()} · 正在读取设备…`;
  identity.classList.add("busy");
  if (options.clearFirst) {
    fillIdentity({}, { flash: false });
  }
  try {
    const data = await api(`/api/env/device?link=${encodeURIComponent(link)}`);
    if (seq !== refreshSeq) return data;
    if (!data.devices || !data.devices.length) {
      pill.textContent = data.message || "没有手机";
      fillIdentity({}, { flash: true });
      setIdentityStatus(data.message || "没有手机", "bad");
      toast(data.message || "没有手机", "bad");
      return data;
    }
    if (data.devices.length !== 1) {
      pill.textContent = data.message || "需要一台手机";
      fillIdentity({}, { flash: true });
      setIdentityStatus(data.message || "需要一台手机", "bad");
      toast(data.message || "需要一台手机", "bad");
      return data;
    }
    const item = data.devices[0];
    fillIdentity(item, { flash: true });
    const phone = item.phone_model || item.model || "未知型号";
    const app = item.app_version ? ` · App ${item.app_version}` : "";
    const wear = item.device_model ? ` · ${item.device_model}` : "";
    pill.textContent = `${phone} · Android ${item.android || "?"}${app}${wear}`;
    pill.classList.add("flash");
    setTimeout(() => pill.classList.remove("flash"), 800);
    setIdentityStatus(options.reason ? `${options.reason}完成` : "已更新", "ok");
    if (options.force || options.clearFirst) {
      toast(`${currentLinkName()} 设备信息已更新`, "ok");
    }
    return data;
  } catch (error) {
    if (seq !== refreshSeq) return;
    pill.textContent = `读取失败：${error.message || error}`;
    setIdentityStatus("读取失败", "bad");
    toast(`设备读取失败：${error.message || error}`, "bad");
    throw error;
  } finally {
    if (seq === refreshSeq) {
      pill.classList.remove("busy");
      identity.classList.remove("busy");
    }
  }
}

function fillIdentity(item, options = {}) {
  const fields = {
    "#manual-phone": item.phone_model || item.model || "",
    "#manual-android": item.android || "",
    "#manual-app": item.app_version || "",
    "#manual-device": item.device_model || "",
    "#manual-hash": item.githash || "",
  };
  Object.entries(fields).forEach(([selector, value]) => {
    const node = document.querySelector(selector);
    if (!node) return;
    const changed = node.value !== value;
    node.value = value;
    if (options.flash && changed) {
      node.classList.remove("updated");
      void node.offsetWidth;
      node.classList.add("updated");
      setTimeout(() => node.classList.remove("updated"), 900);
    }
  });
}

function setIdentityStatus(text, kind) {
  const node = document.querySelector("#identity-status");
  if (!node) return;
  node.textContent = text || "";
  node.className = `status-dot${kind ? ` ${kind}` : ""}`;
}

function currentLinkModules() {
  const item = linkCatalog.find((entry) => entry.id === document.querySelector("#link").value);
  return (item && item.firmware_modules) || [];
}

function renderFirmwareTargets() {
  const modules = currentLinkModules();
  const heading = document.querySelector("#firmware-heading");
  const box = document.querySelector("#firmware-targets");
  if (!modules.length) {
    heading.hidden = true;
    box.hidden = true;
    firmwareTargets = [];
    return;
  }
  heading.hidden = false;
  box.hidden = false;
  const item = linkCatalog.find((entry) => entry.id === document.querySelector("#link").value) || {};
  const defaults = (item.firmware_targets || []).map(String);
  firmwareTargets = defaults.length ? defaults : [String(modules[0].id)];
  box.innerHTML = modules.map((module) => `
    <label class="firmware-choice"><input type="checkbox" data-firmware="${module.id}" ${firmwareTargets.includes(String(module.id)) ? "checked" : ""}> ${module.label}</label>`).join("") +
    `<button id="firmware-all" class="secondary" type="button">全选固件</button>`;
  box.querySelectorAll("input[data-firmware]").forEach((node) => node.addEventListener("change", () => {
    firmwareTargets = [...box.querySelectorAll("input[data-firmware]:checked")].map((input) => input.dataset.firmware);
  }));
  box.querySelector("#firmware-all").addEventListener("click", () => {
    box.querySelectorAll("input[data-firmware]").forEach((input) => { input.checked = true; });
    firmwareTargets = modules.map((module) => String(module.id));
    toast("已全选固件模块");
  });
}

function renderCases() {
  const box = document.querySelector("#cases");
  box.innerHTML = caseCatalog.map((item) => `
    <div class="case-row" data-id="${item.id}" data-strategy="${item.strategy || ""}">
      <input type="checkbox" ${item.default_checked === false ? "" : "checked"}>
      <span class="tag ${item.direction === "逆向" ? "reverse" : "forward"}">${item.direction || "正向"}</span>
      <div>
        <div class="title">${item.id} ${item.name}</div>
        <div class="hint">${item.pass_rule || ""}</div>
      </div>
      <div>
        <label class="loops">运行次数</label>
        <input class="loops" type="number" min="1" max="10000" value="1">
      </div>
    </div>`).join("");
}

function setChecked(checked) {
  document.querySelectorAll("#cases .case-row input[type='checkbox']").forEach((node) => {
    node.checked = checked;
  });
}

function selectedPlan() {
  return [...document.querySelectorAll("#cases .case-row")].flatMap((row) => {
    const box = row.querySelector("input[type='checkbox']");
    if (!box.checked) return [];
    const loops = Number(row.querySelector("input.loops").value || 1);
    return [{ id: row.dataset.id, loops: loops < 1 ? 1 : loops }];
  });
}

async function startTask(button) {
  const cases = selectedPlan();
  if (!cases.length) {
    setVerdict("至少勾选一个用例", true);
    toast("至少勾选一个用例", "bad");
    return;
  }
  // 只有这条链路真的配了固件模块，才要求选。DaFit 没有模块，不该被这件事挡住。
  if (currentLinkModules().length && !firmwareTargets.length) {
    setVerdict("至少选择一个固件模块", true);
    toast("至少选择一个固件模块", "bad");
    return;
  }
  const payload = {
    link: document.querySelector("#link").value,
    cases,
    drop_window: Number(document.querySelector("#drop").value || 120),
    reconnect_timeout: Number(document.querySelector("#timeout").value || 180),
    manual: {
      phone_model: document.querySelector("#manual-phone").value.trim(),
      android: document.querySelector("#manual-android").value.trim(),
      device_model: document.querySelector("#manual-device").value.trim(),
      githash: document.querySelector("#manual-hash").value.trim(),
      app_version: document.querySelector("#manual-app").value.trim(),
    },
  };
  // 没有固件模块的链路（DaFit）不要带这个字段，后端才会用自己的默认值。
  // 带空数组过去会被判成「至少选择一个固件模块」。
  if (currentLinkModules().length) {
    payload.firmware_targets = firmwareTargets;
  }
  const run = async () => {
    const result = await postJson("/api/tasks", payload);
    if (!result.ok) {
      setVerdict(result.message || "启动失败", true);
      toast(result.message || "启动失败", "bad");
      return;
    }
    currentId = result.task.task_id;
    logOffset = 0;
    document.querySelector("#log").textContent = "";
    setVerdict("任务已开始", false);
    toast(`任务已开始 · ${currentLinkName()}`, "ok");
    poll();
  };
  if (button) {
    await withBusy(button, run, "启动中…");
  } else {
    await run();
  }
}

async function stopTask(button) {
  if (!currentId) {
    toast("当前没有运行中的任务", "bad");
    return;
  }
  await withBusy(button, async () => {
    await postJson(`/api/tasks/${currentId}/stop`);
    toast("已请求停止任务");
    setVerdict("正在停止…", false);
  }, "停止中…");
}

async function resumeTask(button) {
  if (!currentId) {
    toast("没有可继续的任务", "bad");
    return;
  }
  await withBusy(button, async () => {
    const result = await postJson(`/api/tasks/${currentId}/resume`);
    if (!result.ok) {
      setVerdict(result.message || "无法继续", true);
      toast(result.message || "无法继续", "bad");
      return;
    }
    toast("已继续任务", "ok");
    poll();
  }, "继续中…");
}

async function cleanup(button) {
  await withBusy(button, async () => {
    const result = await postJson("/api/system/cleanup");
    setVerdict(result.ok ? "临时目录已清空并重建" : "清空失败", !result.ok);
    toast(result.ok ? "临时目录已清空" : "清空失败", result.ok ? "ok" : "bad");
  }, "清理中…");
}

async function poll() {
  if (timer) clearTimeout(timer);
  if (!currentId) return;
  const status = await api(`/api/tasks/${currentId}`);
  if (status.ok) renderTask(status.task);
  const logs = await api(`/api/tasks/${currentId}/log?offset=${logOffset}`);
  if (logs.ok) {
    logOffset = logs.offset;
    if (logs.lines && logs.lines.length) {
      const box = document.querySelector("#log");
      box.textContent += logs.lines.join("\n") + "\n";
      box.scrollTop = box.scrollHeight;
    }
  }
  const done = status.ok && ["finished", "hung", "stopped", "error"].includes(status.task.status);
  if (done) {
    await loadReports();
    toast(`任务结束：${status.task.verdict || status.task.status}`, status.task.verdict === "HUNG" ? "bad" : "ok");
    return;
  }
  timer = setTimeout(poll, 1000);
}

function renderTask(task) {
  document.querySelector("#progress").textContent =
    `${task.link_name} / ${task.strategy} · ${task.case_id || "-"} ${task.case_name || ""} 第 ${task.case_round || 0}/${task.case_loops || 0} 次 · 总进度 ${task.round_index}/${task.round_total} · ${task.status}`;
  renderSteps(task.state);
  document.querySelector("#c-pass").textContent = task.counts.PASS || 0;
  document.querySelector("#c-fail").textContent = task.counts.FAIL_UPGRADE || 0;
  document.querySelector("#c-hung").textContent = task.counts.HUNG_DEVICE || 0;
  document.querySelector("#c-skip").textContent = task.counts.SKIP || 0;
  const bad = ["hung", "error"].includes(task.status) || task.verdict === "HUNG";
  const text = task.message || task.verdict || task.status;
  setVerdict(text, bad);
}

function renderSteps(current) {
  const index = STEPS.indexOf(current);
  document.querySelector("#steps").innerHTML = STEPS.map((step, position) =>
    `<i class="${position <= index ? "on" : ""}">${step}</i>`).join("");
}

function setVerdict(text, bad) {
  const node = document.querySelector("#verdict");
  node.textContent = text || "";
  node.className = bad ? "verdict bad" : (text ? "verdict ok" : "verdict");
}

async function switchCheck(button) {
  setVerdict("正在自检：会关开一次蓝牙和 Wi-Fi，并恢复原状…", false);
  await withBusy(button, async () => {
    const data = await api("/api/env/switch_check", { method: "POST" });
    const lines = (data.checks || []).map((item) => `${item.name}: ${item.ok ? "OK" : "不可用"} ${item.detail || ""}`);
    const message = lines.join(" · ") || data.message || "自检完成";
    setVerdict(message, !data.ok);
    toast(message, data.ok ? "ok" : "bad");
  }, "自检中…");
}

async function withBusy(button, fn, label) {
  if (!button) {
    await fn();
    return;
  }
  const original = button.dataset.label || button.textContent;
  button.dataset.label = original;
  button.disabled = true;
  button.classList.add("busy");
  if (label) button.textContent = label;
  try {
    await fn();
  } finally {
    button.classList.remove("busy");
    button.textContent = button.dataset.label || original;
    button.disabled = false;
  }
}

function toast(message, kind) {
  const node = document.querySelector("#toast");
  if (!node || !message) return;
  node.hidden = false;
  node.textContent = message;
  node.className = `toast show${kind ? ` ${kind}` : ""}`;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    node.classList.remove("show");
    setTimeout(() => { node.hidden = true; }, 220);
  }, 2600);
}

document.addEventListener("DOMContentLoaded", boot);
