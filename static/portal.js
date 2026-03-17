const REVEALED_API_KEY_STORAGE_KEY = "fcs_portal_revealed_api_keys";
const DEFAULT_PAGE_LIMIT = 20;

const state = {
  summary: null,
  user: null,
  workspace: null,
  page: "dashboard",
  revealedApiKeys: {},
  apiKeyModal: {
    name: "",
    value: "",
  },
  transactions: {
    items: [],
    limit: DEFAULT_PAGE_LIMIT,
    offset: 0,
    total: 0,
    has_prev: false,
    has_next: false,
    loaded: false,
  },
  logs: {
    items: [],
    limit: DEFAULT_PAGE_LIMIT,
    offset: 0,
    total: 0,
    has_prev: false,
    has_next: false,
    loaded: false,
  },
};

const pageMetaMap = {
  dashboard: {
    eyebrow: "概览",
    title: "我的次数与使用情况",
    desc: "集中查看当前账号的剩余次数、调用成功率和最近一次充值结果。",
    shortTitle: "概览",
  },
  apiKeys: {
    eyebrow: "API Key",
    title: "个人 API Key 工作区",
    desc: "申请、启停并复制自己的 API Key，完整 Key 仅在创建后返回并在本页缓存显示。",
    shortTitle: "API Key",
  },
  redeem: {
    eyebrow: "充值中心",
    title: "充值与消费记录",
    desc: "兑换管理员发放的 CDK，并回看最近的充值结果和每一次额度变化。",
    shortTitle: "充值中心",
  },
  logs: {
    eyebrow: "调用记录",
    title: "接口调用日志",
    desc: "筛选查看当前账号下的调用状态、项目标识和失败原因，便于排查接入问题。",
    shortTitle: "调用记录",
  },
  account: {
    eyebrow: "账号信息",
    title: "当前登录账号",
    desc: "查看当前用户名称、剩余次数和账号基础信息，不涉及管理员侧数据。",
    shortTitle: "账号信息",
  },
};

const dom = {
  byId(id) {
    return document.getElementById(id);
  },
};

function setText(id, value) {
  const element = dom.byId(id);
  if (element) {
    element.textContent = value;
  }
}

function showBlock(id, visible) {
  const element = dom.byId(id);
  if (!element) {
    return;
  }
  element.classList.toggle("hidden-block", !visible);
}

function showNotice(id, message, kind = "info") {
  const element = dom.byId(id);
  if (!element) {
    return;
  }
  element.className = `notice ${kind}`;
  element.textContent = message;
  element.classList.remove("hidden-block");
}

function formatDateTime(value) {
  if (!value) {
    return "--";
  }
  const normalized = String(value).replace(" ", "T");
  const date = new Date(normalized.endsWith("Z") ? normalized : `${normalized}Z`);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return date.toLocaleString("zh-CN", { hour12: false });
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function statusClass(status) {
  const text = String(status || "").toLowerCase();
  if (text.includes("success")) {
    return "success";
  }
  if (text.includes("fail") || text.includes("error")) {
    return "error";
  }
  if (text.includes("cancel") || text.includes("timeout")) {
    return "warning";
  }
  return "info";
}

function isAuthenticated() {
  return !!state.user;
}

function resetTransactionsState() {
  state.transactions = {
    items: [],
    limit: DEFAULT_PAGE_LIMIT,
    offset: 0,
    total: 0,
    has_prev: false,
    has_next: false,
    loaded: false,
  };
}

function resetLogsState() {
  state.logs = {
    items: [],
    limit: DEFAULT_PAGE_LIMIT,
    offset: 0,
    total: 0,
    has_prev: false,
    has_next: false,
    loaded: false,
  };
}

function loadRevealedApiKeys() {
  try {
    const raw = sessionStorage.getItem(REVEALED_API_KEY_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    state.revealedApiKeys = parsed && typeof parsed === "object" ? parsed : {};
  } catch (_) {
    state.revealedApiKeys = {};
  }
}

function persistRevealedApiKeys() {
  sessionStorage.setItem(REVEALED_API_KEY_STORAGE_KEY, JSON.stringify(state.revealedApiKeys || {}));
}

function cacheRevealedApiKey(id, rawKey) {
  const normalizedId = String(id || "").trim();
  const normalizedKey = String(rawKey || "").trim();
  if (!normalizedId || !normalizedKey) {
    return;
  }
  state.revealedApiKeys[normalizedId] = normalizedKey;
  persistRevealedApiKeys();
}

function getCachedApiKey(id) {
  const normalizedId = String(id || "").trim();
  if (!normalizedId) {
    return "";
  }
  return String(state.revealedApiKeys?.[normalizedId] || "");
}

async function copyText(text) {
  const normalized = String(text || "");
  if (!normalized) {
    throw new Error("没有可复制的内容");
  }
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(normalized);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = normalized;
  textarea.setAttribute("readonly", "readonly");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  document.body.removeChild(textarea);
}

async function requestJson(url, options = {}) {
  const headers = {
    Accept: "application/json",
    ...(options.headers || {}),
  };
  let body;
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }

  const response = await fetch(url, {
    method: options.method || "GET",
    headers,
    body,
    credentials: "same-origin",
  });

  const rawText = await response.text();
  let payload = {};
  if (rawText) {
    try {
      payload = JSON.parse(rawText);
    } catch (_) {
      payload = { raw: rawText };
    }
  }

  if (!response.ok) {
    throw new Error(payload.detail || payload.message || rawText || `HTTP ${response.status}`);
  }
  return payload;
}

function renderAuthTab(tab) {
  document.querySelectorAll(".auth-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.authTab === tab);
  });
  dom.byId("loginPane")?.classList.toggle("active", tab === "login");
  dom.byId("registerPane")?.classList.toggle("active", tab === "register");
}

function closeApiKeyModal() {
  const modal = dom.byId("apiKeyModal");
  if (!modal) {
    return;
  }
  document.body.classList.remove("modal-open");
  modal.classList.add("hidden-block");
  modal.classList.remove("show");
  modal.setAttribute("aria-hidden", "true");
}

function openApiKeyModal(name, value) {
  state.apiKeyModal.name = String(name || "--");
  state.apiKeyModal.value = String(value || "");
  setText("apiKeyModalName", state.apiKeyModal.name);
  setText("apiKeyModalValue", state.apiKeyModal.value || "--");

  const modal = dom.byId("apiKeyModal");
  if (!modal) {
    return;
  }
  document.body.classList.add("modal-open");
  modal.classList.remove("hidden-block");
  modal.classList.add("show");
  modal.setAttribute("aria-hidden", "false");
}

function switchPage(page) {
  state.page = page;
  document.querySelectorAll(".nav-btn").forEach((button) => {
    button.classList.toggle("active", button.dataset.page === page);
  });
  ["dashboard", "apiKeys", "redeem", "logs", "account"].forEach((name) => {
    dom.byId(`page${name.charAt(0).toUpperCase()}${name.slice(1)}`)?.classList.toggle("active", name === page);
  });
  const meta = pageMetaMap[page] || pageMetaMap.dashboard;
  setText("workspaceEyebrow", meta.eyebrow);
  setText("workspaceTitle", meta.title);
  setText("workspaceDesc", meta.desc);
  setText("workspacePage", meta.shortTitle);
}

function renderShell() {
  const authenticated = isAuthenticated();
  showBlock("guestView", !authenticated);
  showBlock("appView", authenticated);
  document.body.classList.toggle("portal-authenticated", authenticated);
  if (!authenticated) {
    closeApiKeyModal();
    showBlock("appNotice", false);
  }
}

function getRegisterLocation() {
  return window.location.pathname === "/" ? "/" : "master-portal";
}

function renderSummaryHints() {
  const role = state.summary?.service?.role || state.summary?.meta?.role || "unknown";
  const registerButton = dom.byId("registerSubmitBtn");
  const locationHint = dom.byId("registerLocationHint");

  if (locationHint) {
    locationHint.textContent = role === "master"
      ? "当前会校验是否从主节点门户注册。"
      : "当前不是主节点门户，自注册可能被后端拒绝。";
  }
  if (registerButton) {
    registerButton.disabled = false;
  }
}

async function loadSummary() {
  try {
    state.summary = await requestJson("/api/portal/summary");
  } catch (_) {
    state.summary = null;
  }
  renderSummaryHints();
}

function renderHeader() {
  const user = state.user || {};
  setText("headerTitle", user.username || "当前账号");
  setText("headerSubtitle", "仅展示当前账号自己的信息与操作。");
  setText("headerUsername", user.username || "--");
  setText("headerQuota", `剩余次数 ${user.quota_remaining ?? 0}`);
  setText("workspaceUser", user.username || "--");
}

function renderDashboard() {
  const user = state.workspace?.user || state.user || {};
  const usage = state.workspace?.usage || {};
  const recentRedeems = Array.isArray(state.workspace?.recent_redeems) ? state.workspace.recent_redeems : [];
  const latestRedeem = recentRedeems.length > 0 ? recentRedeems[0] : null;

  setText("statQuotaRemaining", user.quota_remaining ?? 0);
  setText("statQuotaUsed", user.quota_used ?? 0);
  setText("statSolveSuccess", usage.solve_success_total ?? 0);
  setText("statSolveFailed", usage.solve_failed_total ?? 0);
  setText(
    "statSuccessRate",
    usage.success_rate == null ? "--" : `${Number(usage.success_rate).toFixed(2)}%`,
  );
  setText("statLastRequest", formatDateTime(usage.last_request_at));
  setText("usageRuleText", "最终生成成功才扣减 1 次；失败、取消或报错会返还次数。");

  showBlock("latestRedeemEmpty", !latestRedeem);
  showBlock("latestRedeemCard", !!latestRedeem);
  if (latestRedeem) {
    setText("latestRedeemCode", latestRedeem.code || "--");
    setText("latestRedeemQuota", latestRedeem.quota_times ?? "--");
    setText("latestRedeemTime", formatDateTime(latestRedeem.redeemed_at));
  }
}

function renderApiKeys() {
  const tbody = dom.byId("apiKeysTableBody");
  if (!tbody) {
    return;
  }
  const items = Array.isArray(state.workspace?.api_keys) ? state.workspace.api_keys : [];
  if (!items.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-cell">暂无 API Key。</td></tr>';
    return;
  }
  tbody.innerHTML = items.map((item) => {
    const rawKey = getCachedApiKey(item.id);
    const copyLabel = rawKey ? "复制 Key" : "复制前缀";
    return `
      <tr>
        <td>${escapeHtml(item.name || "--")}</td>
        <td>${escapeHtml(item.key_prefix || "--")}</td>
        <td><span class="status-chip ${item.enabled ? "success" : "warning"}">${item.enabled ? "启用中" : "已禁用"}</span></td>
        <td>${escapeHtml(String(item.quota_used ?? 0))}</td>
        <td>${escapeHtml(formatDateTime(item.last_used_at))}</td>
        <td>
          <div class="action-row">
            <button class="btn subtle mini-btn" type="button" data-action="toggle-api-key" data-id="${item.id}" data-enabled="${item.enabled ? 1 : 0}">${item.enabled ? "禁用" : "启用"}</button>
            <button class="btn subtle mini-btn" type="button" data-action="copy-api-key" data-id="${item.id}" data-prefix="${escapeHtml(item.key_prefix || "")}">${copyLabel}</button>
            <button class="btn ghost mini-btn" type="button" data-action="delete-api-key" data-id="${item.id}">软删除</button>
          </div>
        </td>
      </tr>
    `;
  }).join("");
}

function renderTransactionsPager() {
  const infoEl = dom.byId("transactionsPagerInfo");
  const prevEl = dom.byId("transactionsPrevBtn");
  const nextEl = dom.byId("transactionsNextBtn");
  if (!infoEl || !prevEl || !nextEl) {
    return;
  }
  const limit = Math.max(1, Number(state.transactions.limit || DEFAULT_PAGE_LIMIT));
  const offset = Math.max(0, Number(state.transactions.offset || 0));
  const total = Math.max(0, Number(state.transactions.total || 0));
  const page = Math.floor(offset / limit) + 1;
  const totalPages = total > 0 ? Math.ceil(total / limit) : 1;
  const from = total > 0 ? offset + 1 : 0;
  const to = total > 0 ? Math.min(offset + limit, total) : 0;
  infoEl.textContent = total > 0
    ? `第 ${page}/${totalPages} 页，显示 ${from}-${to}，共 ${total} 条`
    : "暂无消费记录。";
  prevEl.disabled = !state.transactions.has_prev;
  nextEl.disabled = !state.transactions.has_next;
}

function renderTransactions() {
  const tbody = dom.byId("transactionsTableBody");
  if (!tbody) {
    return;
  }
  const items = state.transactions.loaded
    ? state.transactions.items
    : (Array.isArray(state.workspace?.recent_transactions) ? state.workspace.recent_transactions : []);
  if (!items.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty-cell">暂无消费记录。</td></tr>';
    renderTransactionsPager();
    return;
  }
  tbody.innerHTML = items.map((item) => {
    const amount = Number(item.change_amount || 0);
    return `
      <tr>
        <td>${escapeHtml(formatDateTime(item.created_at))}</td>
        <td>${escapeHtml(amount > 0 ? `+${amount}` : String(amount))}</td>
        <td>${escapeHtml(String(item.balance_after ?? "--"))}</td>
        <td>${escapeHtml(item.source_type || "--")}</td>
        <td>${escapeHtml(item.note || item.source_ref || "-")}</td>
      </tr>
    `;
  }).join("");
  renderTransactionsPager();
}

function renderRedeems() {
  const tbody = dom.byId("redeemTableBody");
  if (!tbody) {
    return;
  }
  const items = Array.isArray(state.workspace?.recent_redeems) ? state.workspace.recent_redeems : [];
  if (!items.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-cell">暂无兑换记录。</td></tr>';
    return;
  }
  tbody.innerHTML = items.map((item) => `
    <tr>
      <td>${escapeHtml(item.code || "--")}</td>
      <td>${escapeHtml(String(item.quota_times ?? "--"))}</td>
      <td>${escapeHtml(formatDateTime(item.redeemed_at))}</td>
      <td>${escapeHtml(item.note || "-")}</td>
    </tr>
  `).join("");
}

function renderLogsPager() {
  const infoEl = dom.byId("logsPagerInfo");
  const prevEl = dom.byId("logsPrevBtn");
  const nextEl = dom.byId("logsNextBtn");
  if (!infoEl || !prevEl || !nextEl) {
    return;
  }
  const limit = Math.max(1, Number(state.logs.limit || DEFAULT_PAGE_LIMIT));
  const offset = Math.max(0, Number(state.logs.offset || 0));
  const total = Math.max(0, Number(state.logs.total || 0));
  const page = Math.floor(offset / limit) + 1;
  const totalPages = total > 0 ? Math.ceil(total / limit) : 1;
  const from = total > 0 ? offset + 1 : 0;
  const to = total > 0 ? Math.min(offset + limit, total) : 0;
  infoEl.textContent = total > 0
    ? `第 ${page}/${totalPages} 页，显示 ${from}-${to}，共 ${total} 条`
    : "暂无日志记录。";
  prevEl.disabled = !state.logs.has_prev;
  nextEl.disabled = !state.logs.has_next;
}

function renderLogs() {
  const tbody = dom.byId("logsTableBody");
  if (!tbody) {
    return;
  }
  const items = state.logs.items || [];
  if (!items.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="empty-cell">暂无日志记录。</td></tr>';
    renderLogsPager();
    return;
  }
  tbody.innerHTML = items.map((item) => `
    <tr>
      <td>${escapeHtml(formatDateTime(item.created_at))}</td>
      <td><span class="status-chip ${statusClass(item.status)}">${escapeHtml(item.status || "--")}</span></td>
      <td>${escapeHtml(item.project_id || "--")}</td>
      <td>${escapeHtml(item.action || "--")}</td>
      <td>${escapeHtml(item.api_key_prefix || item.api_key_name || "--")}</td>
      <td>${escapeHtml(item.error_reason || "-")}</td>
    </tr>
  `).join("");
  renderLogsPager();
}

function renderAccount() {
  const user = state.workspace?.user || state.user || {};
  setText("accountUsernameValue", user.username || "--");
  setText("accountRegisteredValue", isAuthenticated() ? "已注册" : "未注册");
  setText("accountLocationValue", user.register_location || "--");
  setText("accountEnabledValue", user.enabled ? "启用中" : "已禁用");
  setText("accountCreatedAtValue", formatDateTime(user.created_at));
  setText("accountLastLoginValue", formatDateTime(user.last_login_at));
}

function renderWorkspace() {
  renderShell();
  if (!isAuthenticated()) {
    return;
  }
  renderHeader();
  renderDashboard();
  renderApiKeys();
  renderRedeems();
  renderTransactions();
  renderLogs();
  renderAccount();
}

function applyPagedPayload(target, payload, fallbackLimit, fallbackOffset) {
  const items = Array.isArray(payload?.items) ? payload.items : [];
  const limit = Math.max(1, Number(payload?.limit || fallbackLimit || DEFAULT_PAGE_LIMIT));
  const offset = Math.max(0, Number(payload?.offset || fallbackOffset || 0));
  const totalRaw = Number(payload?.total);
  const total = Number.isFinite(totalRaw)
    ? Math.max(0, totalRaw)
    : Math.max(offset + items.length, items.length);
  target.items = items;
  target.limit = limit;
  target.offset = offset;
  target.total = total;
  target.has_prev = Boolean(payload?.has_prev) || offset > 0;
  target.has_next = Boolean(payload?.has_next) || offset + limit < total;
  target.loaded = true;
}

async function loadAuthMe() {
  try {
    const payload = await requestJson("/api/portal/auth/me");
    if (payload.authenticated) {
      state.workspace = payload;
      state.user = payload.user || null;
    } else {
      state.workspace = null;
      state.user = null;
      resetTransactionsState();
      resetLogsState();
    }
  } catch (_) {
    state.workspace = null;
    state.user = null;
    resetTransactionsState();
    resetLogsState();
  }
  renderWorkspace();
}

async function loadWorkspace(showNoticeMessage = false) {
  if (!isAuthenticated()) {
    return;
  }
  state.workspace = await requestJson("/api/portal/user/overview");
  state.user = state.workspace.user || state.user;
  renderWorkspace();
  if (showNoticeMessage) {
    showNotice("appNotice", "数据已刷新。", "success");
  }
}

async function loadTransactions(showNoticeMessage = false) {
  if (!isAuthenticated()) {
    resetTransactionsState();
    renderTransactions();
    return;
  }
  const limit = Math.max(1, Number(state.transactions.limit || DEFAULT_PAGE_LIMIT));
  const offset = Math.max(0, Number(state.transactions.offset || 0));
  const payload = await requestJson(`/api/portal/user/transactions?limit=${limit}&offset=${offset}`);
  applyPagedPayload(state.transactions, payload, limit, offset);
  renderTransactions();
  if (showNoticeMessage) {
    showNotice("redeemNotice", "次数变化明细已刷新。", "success");
  }
}

async function loadLogs(showNoticeMessage = false) {
  if (!isAuthenticated()) {
    resetLogsState();
    renderLogs();
    return;
  }
  const status = String(dom.byId("logStatusFilter")?.value || "").trim();
  const projectId = String(dom.byId("logProjectFilter")?.value || "").trim();
  const limit = Math.max(1, Number(state.logs.limit || DEFAULT_PAGE_LIMIT));
  const offset = Math.max(0, Number(state.logs.offset || 0));
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  if (status) {
    params.set("status", status);
  }
  if (projectId) {
    params.set("project_id", projectId);
  }
  const payload = await requestJson(`/api/portal/user/logs?${params.toString()}`);
  applyPagedPayload(state.logs, payload, limit, offset);
  renderLogs();
  if (showNoticeMessage) {
    showNotice("appNotice", "日志已刷新。", "success");
  }
}

async function checkRegisterStatus(showSuccess = false) {
  const username = String(dom.byId("registerUsername")?.value || "").trim();
  if (!username) {
    showNotice("registerCheckHint", "输入用户名后会自动检查是否已注册。", "info");
    return null;
  }
  const result = await requestJson(`/api/portal/auth/check?username=${encodeURIComponent(username)}`);
  if (result.registered) {
    showNotice("registerCheckHint", result.message || "该用户名已注册，请直接登录。", "warning");
  } else if (showSuccess) {
    showNotice("registerCheckHint", result.message || "该用户名尚未注册，可以继续创建账号。", "success");
  } else {
    showNotice("registerCheckHint", result.message || "该用户名尚未注册。", "info");
  }
  return result;
}

async function handleLogin(event) {
  event.preventDefault();
  const username = String(dom.byId("loginUsername")?.value || "").trim();
  const password = String(dom.byId("loginPassword")?.value || "");
  if (!username || !password) {
    throw new Error("请输入用户名和密码");
  }
  await requestJson("/api/portal/auth/login", {
    method: "POST",
    body: { username, password },
  });
  await loadAuthMe();
  await loadWorkspace(false);
  resetTransactionsState();
  resetLogsState();
  await loadTransactions(false);
  await loadLogs(false);
  switchPage("dashboard");
  showNotice("appNotice", "登录成功。", "success");
}

async function handleRegister(event) {
  event.preventDefault();
  const username = String(dom.byId("registerUsername")?.value || "").trim();
  const password = String(dom.byId("registerPassword")?.value || "");
  if (!username || !password) {
    throw new Error("用户名和密码不能为空");
  }
  if ((state.summary?.service?.role || state.summary?.meta?.role) === "subnode") {
    throw new Error("当前入口不开放用户自注册");
  }
  const checked = await checkRegisterStatus(false);
  if (checked?.registered) {
    throw new Error(checked.message || "该用户名已注册，请直接登录");
  }
  await requestJson("/api/portal/auth/register", {
    method: "POST",
    body: {
      username,
      password,
      register_location: getRegisterLocation(),
    },
  });
  await loadAuthMe();
  await loadWorkspace(false);
  resetTransactionsState();
  resetLogsState();
  await loadTransactions(false);
  await loadLogs(false);
  switchPage("dashboard");
  showNotice("appNotice", `注册成功，当前账号 ${username} 已登录。`, "success");
}

async function handleLogout() {
  await requestJson("/api/portal/auth/logout", { method: "POST" });
  state.user = null;
  state.workspace = null;
  resetTransactionsState();
  resetLogsState();
  closeApiKeyModal();
  renderShell();
  renderAuthTab("login");
  showNotice("guestNotice", "你已退出登录。", "info");
}

async function handleCreateApiKey(event) {
  event.preventDefault();
  const name = String(dom.byId("newApiKeyName")?.value || "").trim();
  if (!name) {
    throw new Error("请填写 API Key 名称");
  }
  const result = await requestJson("/api/portal/user/api-keys", {
    method: "POST",
    body: { name },
  });
  dom.byId("newApiKeyName").value = "";
  cacheRevealedApiKey(result.item?.id, result.api_key || "");
  await loadWorkspace(false);
  openApiKeyModal(result.item?.name || name, result.api_key || "");
  showNotice("apiKeyNotice", result.message || "API Key 已创建。", "success");
}

async function handleApiKeyAction(action, apiKeyId, enabled) {
  if (action === "toggle") {
    const result = await requestJson(`/api/portal/user/api-keys/${apiKeyId}`, {
      method: "PATCH",
      body: { enabled: !enabled },
    });
    await loadWorkspace(false);
    showNotice("apiKeyNotice", `API Key ${result.item?.enabled ? "已启用" : "已禁用"}。`, "success");
    return;
  }
  if (action === "delete") {
    await requestJson(`/api/portal/user/api-keys/${apiKeyId}`, { method: "DELETE" });
    await loadWorkspace(false);
    showNotice("apiKeyNotice", `API Key #${apiKeyId} 已软删除。`, "warning");
  }
}

async function handleCopyApiKey(apiKeyId, prefix) {
  const rawKey = getCachedApiKey(apiKeyId);
  if (rawKey) {
    await copyText(rawKey);
    showNotice("apiKeyNotice", `API Key #${apiKeyId} 已复制。`, "success");
    return;
  }
  if (prefix) {
    await copyText(prefix);
    showNotice("apiKeyNotice", `完整 Key 未缓存，已复制前缀 ${prefix}。`, "warning");
    return;
  }
  throw new Error("当前没有可复制的 Key 内容");
}

async function handleRedeem(event) {
  event.preventDefault();
  const code = String(dom.byId("redeemCodeInput")?.value || "").trim();
  if (!code) {
    throw new Error("请输入兑换码");
  }
  const result = await requestJson("/api/portal/redeem", {
    method: "POST",
    body: { code },
  });
  dom.byId("redeemCodeInput").value = "";
  await loadWorkspace(false);
  resetTransactionsState();
  await loadTransactions(false);
  showNotice("redeemNotice", result.message || "兑换成功。", "success");
  showNotice("appNotice", result.message || "兑换成功。", "success");
}

async function handleCopyTransactionsCurrentPage() {
  const items = state.transactions.items || [];
  if (!items.length) {
    throw new Error("当前页没有可复制的明细");
  }
  const text = items.map((item) => {
    const amount = Number(item.change_amount || 0);
    const formattedAmount = amount > 0 ? `+${amount}` : String(amount);
    return [
      formatDateTime(item.created_at),
      `变化:${formattedAmount}`,
      `余额:${item.balance_after ?? "--"}`,
      `来源:${item.source_type || "--"}`,
      `说明:${item.note || item.source_ref || "-"}`,
    ].join(" | ");
  }).join("\n");
  await copyText(text);
  showNotice("redeemNotice", "当前页次数变化明细已复制。", "success");
}

function wireEvents() {
  dom.byId("tabLoginBtn")?.addEventListener("click", () => renderAuthTab("login"));
  dom.byId("tabRegisterBtn")?.addEventListener("click", () => renderAuthTab("register"));

  dom.byId("loginForm")?.addEventListener("submit", async (event) => {
    try {
      await handleLogin(event);
    } catch (error) {
      showNotice("guestNotice", error.message || "登录失败", "error");
    }
  });

  dom.byId("registerUsername")?.addEventListener("blur", async () => {
    try {
      await checkRegisterStatus(false);
    } catch (_) {}
  });

  dom.byId("registerForm")?.addEventListener("submit", async (event) => {
    try {
      await handleRegister(event);
    } catch (error) {
      showNotice("guestNotice", error.message || "注册失败", "error");
    }
  });

  dom.byId("logoutBtn")?.addEventListener("click", async () => {
    try {
      await handleLogout();
    } catch (error) {
      showNotice("appNotice", error.message || "退出失败", "error");
    }
  });

  document.querySelectorAll(".nav-btn[data-page]").forEach((button) => {
    button.addEventListener("click", async () => {
      const page = button.dataset.page || "dashboard";
      switchPage(page);
      if (page === "logs") {
        try {
          await loadLogs(false);
        } catch (error) {
          showNotice("appNotice", error.message || "日志加载失败", "error");
        }
      }
      if (page === "redeem") {
        try {
          await loadTransactions(false);
        } catch (error) {
          showNotice("redeemNotice", error.message || "明细加载失败", "error");
        }
      }
    });
  });

  dom.byId("refreshUserBtn")?.addEventListener("click", async () => {
    try {
      await loadWorkspace(true);
      if (state.page === "redeem") {
        await loadTransactions(false);
      }
      if (state.page === "logs") {
        await loadLogs(false);
      }
    } catch (error) {
      showNotice("appNotice", error.message || "刷新失败", "error");
    }
  });

  dom.byId("refreshApiKeysBtn")?.addEventListener("click", async () => {
    try {
      await loadWorkspace(true);
      switchPage("apiKeys");
      showNotice("apiKeyNotice", "API Key 列表已刷新。", "success");
    } catch (error) {
      showNotice("apiKeyNotice", error.message || "刷新 API Key 失败", "error");
    }
  });

  dom.byId("createApiKeyForm")?.addEventListener("submit", async (event) => {
    try {
      await handleCreateApiKey(event);
      switchPage("apiKeys");
    } catch (error) {
      showNotice("apiKeyNotice", error.message || "申请 API Key 失败", "error");
    }
  });

  dom.byId("redeemForm")?.addEventListener("submit", async (event) => {
    try {
      await handleRedeem(event);
    } catch (error) {
      showNotice("redeemNotice", error.message || "兑换失败", "error");
    }
  });

  dom.byId("copyTransactionsBtn")?.addEventListener("click", async () => {
    try {
      await handleCopyTransactionsCurrentPage();
    } catch (error) {
      showNotice("redeemNotice", error.message || "复制失败", "error");
    }
  });

  const refreshLogs = async () => {
    state.logs.offset = 0;
    try {
      await loadLogs(true);
    } catch (error) {
      showNotice("appNotice", error.message || "日志刷新失败", "error");
    }
  };
  dom.byId("refreshLogsBtn")?.addEventListener("click", refreshLogs);
  dom.byId("logStatusFilter")?.addEventListener("change", refreshLogs);
  dom.byId("logProjectFilter")?.addEventListener("keydown", async (event) => {
    if (event.key !== "Enter") {
      return;
    }
    event.preventDefault();
    await refreshLogs();
  });

  dom.byId("transactionsPrevBtn")?.addEventListener("click", async () => {
    if (!state.transactions.has_prev) {
      return;
    }
    state.transactions.offset = Math.max(0, state.transactions.offset - state.transactions.limit);
    try {
      await loadTransactions(false);
    } catch (error) {
      state.transactions.offset += state.transactions.limit;
      showNotice("redeemNotice", error.message || "上一页加载失败", "error");
    }
  });

  dom.byId("transactionsNextBtn")?.addEventListener("click", async () => {
    if (!state.transactions.has_next) {
      return;
    }
    state.transactions.offset += state.transactions.limit;
    try {
      await loadTransactions(false);
    } catch (error) {
      state.transactions.offset = Math.max(0, state.transactions.offset - state.transactions.limit);
      showNotice("redeemNotice", error.message || "下一页加载失败", "error");
    }
  });

  dom.byId("logsPrevBtn")?.addEventListener("click", async () => {
    if (!state.logs.has_prev) {
      return;
    }
    state.logs.offset = Math.max(0, state.logs.offset - state.logs.limit);
    try {
      await loadLogs(false);
    } catch (error) {
      state.logs.offset += state.logs.limit;
      showNotice("appNotice", error.message || "上一页加载失败", "error");
    }
  });

  dom.byId("logsNextBtn")?.addEventListener("click", async () => {
    if (!state.logs.has_next) {
      return;
    }
    state.logs.offset += state.logs.limit;
    try {
      await loadLogs(false);
    } catch (error) {
      state.logs.offset = Math.max(0, state.logs.offset - state.logs.limit);
      showNotice("appNotice", error.message || "下一页加载失败", "error");
    }
  });

  dom.byId("apiKeyModalCloseBtn")?.addEventListener("click", closeApiKeyModal);
  dom.byId("apiKeyModal")?.addEventListener("click", (event) => {
    if (event.target === dom.byId("apiKeyModal")) {
      closeApiKeyModal();
    }
  });
  dom.byId("apiKeyModalCopyBtn")?.addEventListener("click", async () => {
    try {
      await copyText(state.apiKeyModal.value || "");
      showNotice("apiKeyNotice", "完整 Key 已复制。", "success");
    } catch (error) {
      showNotice("apiKeyNotice", error.message || "复制失败", "error");
    }
  });
}

async function bootstrap() {
  loadRevealedApiKeys();
  renderShell();
  renderAuthTab("login");
  renderTransactionsPager();
  renderLogsPager();
  wireEvents();
  await loadSummary();
  await loadAuthMe();
  if (isAuthenticated()) {
    await loadWorkspace(false);
    resetTransactionsState();
    resetLogsState();
    await loadTransactions(false);
    await loadLogs(false);
    switchPage("dashboard");
  }
}

document.addEventListener("click", async (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) {
    return;
  }
  if (target.dataset.action === "toggle-api-key") {
    try {
      await handleApiKeyAction("toggle", Number(target.dataset.id), target.dataset.enabled === "1");
    } catch (error) {
      showNotice("apiKeyNotice", error.message || "更新 API Key 失败", "error");
    }
  }
  if (target.dataset.action === "copy-api-key") {
    try {
      await handleCopyApiKey(Number(target.dataset.id), String(target.dataset.prefix || "").trim());
    } catch (error) {
      showNotice("apiKeyNotice", error.message || "复制 API Key 失败", "error");
    }
  }
  if (target.dataset.action === "delete-api-key") {
    const apiKeyId = Number(target.dataset.id);
    const ok = window.confirm(`确定软删除 API Key #${apiKeyId} 吗？软删除后该 Key 会被禁用。`);
    if (!ok) {
      return;
    }
    try {
      await handleApiKeyAction("delete", apiKeyId, false);
    } catch (error) {
      showNotice("apiKeyNotice", error.message || "软删除 API Key 失败", "error");
    }
  }
});

window.addEventListener("DOMContentLoaded", bootstrap);
