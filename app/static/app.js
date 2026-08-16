/* BidVolt 测试客户端（Issue #5/#10）：多环境配置 + 连接测试 + 全业务流程真实调用 */
let API_BASE = "";
try {
  const cur = JSON.parse(localStorage.getItem("bidvolt_env_cur") || "null");
  API_BASE = cur && cur.base ? String(cur.base).replace(/\/+$/, "") : "";
} catch { API_BASE = ""; }
const API = () => API_BASE + "/api/v1";
/* 凭据按环境隔离（Issue #10 P0.3）：不同 API_BASE 使用独立的 token 键，切换环境即重新登录 */
const ENV_KEY = () => API_BASE || "same-origin";
const TOKEN_KEY = () => `bidvolt_token_${ENV_KEY()}`;
const REFRESH_KEY = () => `bidvolt_refresh_${ENV_KEY()}`;
const USER_KEY = () => `bidvolt_user_${ENV_KEY()}`;
const PROJECT_KEY = () => `bidvolt_project_${ENV_KEY()}`;
const TASK_KEY = () => `bidvolt_task_${ENV_KEY()}`;
let token = localStorage.getItem(TOKEN_KEY()) || "";
let projectId = null;
let deliverables = [];
let calcId = null;
let activeTaskId = null;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function log(msg, cls = "") {
  const pre = $("log");
  const div = document.createElement("div");
  div.className = cls;
  div.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  pre.prepend(div);
}

function clearBusinessContext() {
  /* 项目/任务/报价/编辑等上下文（Issue #10 P1.9/P1.12）：切换项目或环境时全部清理 */
  projectId = null;
  deliverables = [];
  calcId = null;
  activeTaskId = null;
  localStorage.removeItem(PROJECT_KEY());
  localStorage.removeItem(TASK_KEY());
  localStorage.removeItem("bidvolt_calc_" + ENV_KEY());
}

async function tryRefreshToken() {
  const rt = localStorage.getItem(REFRESH_KEY());
  if (!rt) return false;
  try {
    const resp = await fetch(API() + "/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: rt }),
    });
    if (!resp.ok) return false;
    saveAuth(await resp.json());
    return true;
  } catch { return false; }
}

async function rawFetch(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (opts.body && !(opts.body instanceof FormData)) headers["Content-Type"] = "application/json";
  return fetch(API() + path, { ...opts, headers, body: opts.body instanceof FormData ? opts.body : opts.body ? JSON.stringify(opts.body) : undefined });
}

async function api(path, opts = {}) {
  let resp = await rawFetch(path, opts);
  if (resp.status === 401 && token && await tryRefreshToken()) {
    resp = await rawFetch(path, opts);  // 会话过期自动刷新重试一次（Issue #10 P1.13）
  }
  if (resp.status === 204) return null;
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!resp.ok) {
    const fe = data?.field_errors;
    const feText = Array.isArray(fe) && fe.length
      ? "：" + fe.map((f) => `${(f.loc || []).filter((x) => x !== "body").join(".") || "参数"} ${f.msg}`).join("；")
      : "";
    throw new Error(`[HTTP ${resp.status}] ` + (data?.detail || data || "请求失败") + feText);
  }
  return data;
}

async function authedDownload(path, fallbackName = "download") {
  /* 统一带认证下载（Issue #10 P1.7）：Bearer + 当前环境 base，支持 filename */
  try {
    const resp = await rawFetch(path);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const cd = resp.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename\*?=(?:UTF-8''|")?([^";]+)/i);
    a.download = m ? decodeURIComponent(m[1]) : fallbackName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    log(`下载成功：${a.download}`, "ok");
  } catch (e) { log(`下载失败：${e}`, "err"); }
}

function saveAuth(data) {
  token = data.access_token;
  localStorage.setItem(TOKEN_KEY(), token);
  localStorage.setItem(REFRESH_KEY(), data.refresh_token);
  if (data.user_id != null) localStorage.setItem(USER_KEY(), String(data.user_id));
  renderAuth();
}

function renderAuth() {
  $("authbar").innerHTML = token
    ? `<span>已登录 user#${localStorage.getItem(USER_KEY()) || ""} · 环境：${ENV_KEY()}</span> <button class="ghost" onclick="logout()">退出</button>`
    : "<span>未登录</span>";
}

async function logout() {
  try {
    await api("/auth/logout", { method: "POST", body: { refresh_token: localStorage.getItem(REFRESH_KEY()) } });
  } catch {}
  token = "";
  localStorage.removeItem(TOKEN_KEY());
  localStorage.removeItem(REFRESH_KEY());
  localStorage.removeItem(USER_KEY());  // 只清凭据，保留多环境配置（Issue #10 P2）
  clearBusinessContext();
  renderAuth();
  renderPanel("auth");
}

const TABS = [
  ["auth", "认证"], ["project", "项目"], ["material", "资料"], ["req", "要求/匹配"], ["deliverable", "成果"],
  ["task", "任务/评标"], ["quote", "报价"], ["export", "导出"], ["search", "搜索/对话"],
  ["settings", "设置/连接"],
];

const STEP_ORDER = ["auth", "project", "material", "req", "deliverable", "task", "quote", "export", "search"];

function renderSteps(currentTab) {
  const idx = STEP_ORDER.indexOf(currentTab);
  const steps = ["连接", "认证", "项目", "资料", "要求", "成果", "评审", "报价", "导出"];
  $("steps").innerHTML = steps.map((label, i) => {
    const cls = i === idx ? "step now" : (i < idx ? "step done" : "step");
    return `<span class="${cls}">${i + 1}. ${label}</span>`;
  }).join("");
  $("steps").title = "按顺序完成各步骤；未满足前置条件的操作会给出提示";
}

function renderTabs() {
  $("tabs").innerHTML = TABS.map(([id, label]) => `<button data-tab="${id}">${label}</button>`).join("");
  document.querySelectorAll("#tabs button").forEach((b) => b.onclick = () => renderPanel(b.dataset.tab));
}

function renderPanel(tab) {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  renderSteps(tab);
  const fn = { auth: panelAuth, project: panelProject, material: panelMaterial, req: panelRequirements, deliverable: panelDeliverable, task: panelTask, quote: panelQuote, export: panelExport, search: panelSearch, settings: panelSettings }[tab];
  fn();
}

/* ---------- 认证（Issue #10 P1.5：分表单 + 前置校验 + 字段级错误 + 提交防重） ---------- */
function panelAuth() {
  $("panel").innerHTML = `
    <h3>注册 / 登录</h3>
    <div class="row">
      <div class="field-row" style="flex:1"><label>邮箱（必填）</label><input id="a-email" placeholder="you@example.com"></div>
      <div class="field-row" style="flex:1"><label>密码（至少 8 位，且同时包含字母和数字）</label><input id="a-pwd" type="password" placeholder="Abc12345"></div>
      <div class="field-row" style="flex:1"><label>企业名称（仅注册必填）</label><input id="a-name" placeholder="企业名称"></div>
    </div>
    <div class="row">
      <button id="a-register" onclick="doAuth('register')">注册</button>
      <button id="a-login" class="ghost" onclick="doAuth('login')">登录</button>
      <button class="ghost" onclick="me()">查看 /auth/me</button>
    </div>
    <div class="form-hint" id="a-hint"></div>
    <pre id="a-result" class="muted"></pre>`;
}

function _validateAuth(mode) {
  const email = ($("a-email").value || "").trim();
  const pwd = $("a-pwd").value || "";
  const name = ($("a-name").value || "").trim();
  if (!email) return "邮箱不能为空";
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return "邮箱格式不正确";
  if (pwd.length < 8) return "密码至少 8 位";
  if (!/[a-zA-Z]/.test(pwd) || !/\d/.test(pwd)) return "密码需同时包含字母和数字";
  if (mode === "register" && !name) return "注册时企业名称不能为空";
  return null;
}

async function doAuth(mode) {
  const hint = $("a-hint");
  hint.textContent = "";
  const invalid = _validateAuth(mode);
  if (invalid) { hint.textContent = invalid; return; }
  const btn = $(`a-${mode}`);
  btn.disabled = true;  // 提交期间禁用，避免重复请求
  const body = mode === "register"
    ? { email: $("a-email").value.trim(), password: $("a-pwd").value, enterprise_name: $("a-name").value.trim() }
    : { email: $("a-email").value.trim(), password: $("a-pwd").value };
  try {
    const data = await api(`/auth/${mode}`, { method: "POST", body });
    saveAuth(data);
    const masked = { ...data };
    masked.access_token = "••••••（已保存，不展示）";
    masked.refresh_token = "••••••（已保存，不展示）";  // Issue #10 P0：页面不显示完整 Token
    $("a-result").textContent = JSON.stringify(masked, null, 2);
    log(`${mode} 成功（user#${data.user_id}）`, "ok");
    renderPanel("project");  // 登录成功后进入项目选择（Issue #10 P1.6 流程）
  } catch (e) {
    hint.textContent = String(e);
    log(`认证失败：${e}`, "err");
  } finally {
    btn.disabled = false;
  }
}

async function me() {
  try {
    const data = await api("/auth/me");
    $("a-result").textContent = JSON.stringify(data, null, 2);
  } catch (e) { $("a-result").textContent = String(e); }
}

/* ---------- 项目 ---------- */
function panelProject() {
  $("panel").innerHTML = `
    <h3>项目</h3>
    <div class="row"><input id="p-name" placeholder="项目名称（必填）"><input id="p-no" placeholder="招标编号(可选)">
      <button onclick="createProject()">创建项目</button></div>
    <table><thead><tr><th>ID</th><th>名称</th><th>编号</th><th>状态</th><th>操作</th></tr></thead><tbody id="p-rows"></tbody></table>
    <div class="row"><button class="ghost" onclick="loadSnapshots()">快照列表</button>
      <button class="ghost" onclick="loadTasks()">活动任务</button></div>
    <pre id="p-extra" class="muted"></pre>`;
  refreshProjects();
}

async function refreshProjects() {
  try {
    const data = await api("/projects?size=50");
    /* Issue #10 P0.2：不再把用户数据拼入内联 onclick，改用 data-* 绑定 */
    $("p-rows").innerHTML = data.items.map((p) => `
      <tr><td>${p.project_id}</td><td>${esc(p.name)}</td><td>${esc(p.tender_no || "")}</td><td>${p.status}</td>
      <td><button class="row-use" data-id="${p.project_id}" data-name="${esc(p.name)}">选用</button>
          <button class="ghost row-archive" data-id="${p.project_id}">归档</button></td></tr>`).join("");
    document.querySelectorAll("#p-rows .row-use").forEach((b) => {
      b.onclick = () => selectProject(Number(b.dataset.id), b.dataset.name);
    });
    document.querySelectorAll("#p-rows .row-archive").forEach((b) => {
      b.onclick = () => archiveProject(Number(b.dataset.id));
    });
  } catch (e) { log(`项目列表失败：${e}`, "err"); }
}

async function createProject() {
  const name = ($("p-name").value || "").trim();
  if (!name) return log("项目名称不能为空（必填），请先填写项目名称", "err");
  if (name.length > 300) return log("项目名称过长（最多 300 字）", "err");
  try {
    const p = await api("/projects", { method: "POST", body: { name, tender_no: ($("p-no").value || "").trim() || null } });
    selectProject(p.project_id, p.name);
    log(`项目创建成功 #${p.project_id}`, "ok");
  } catch (e) { log(`创建失败：${e}`, "err"); }
}

function selectProject(id, name) {
  clearBusinessContext();  // 切换项目清理全部下游上下文（Issue #10 P1.9）
  projectId = id;
  localStorage.setItem(PROJECT_KEY(), String(id));
  log(`当前项目：${name} (#${id})`, "ok");
  refreshProjects();
}

async function archiveProject(id) {
  try { await api(`/projects/${id}/archive`, { method: "POST" }); log(`项目 ${id} 已归档`, "ok"); refreshProjects(); } catch (e) { log(`归档失败：${e}`, "err"); }
}

async function loadSnapshots() {
  if (!projectId) return log("先选用项目", "err");
  try {
    const data = await api(`/projects/${projectId}/snapshots`);
    $("p-extra").textContent = JSON.stringify(data.items.map((s) => ({
      snapshot_id: s.snapshot_id, type: s.snapshot_type, created_at: s.created_at, input_refs: s.input_refs,
    })), null, 2);
    log(`快照 ${data.items.length} 条`, "ok");
  } catch (e) { log(`快照列表失败：${e}`, "err"); }
}

async function loadTasks() {
  if (!projectId) return log("先选用项目", "err");
  try {
    const data = await api(`/projects/${projectId}/tasks`);
    $("p-extra").textContent = JSON.stringify(data.items.map((t) => ({
      task_id: t.task_id, type: t.task_type, status: t.status, created_at: t.created_at, progress: t.progress,
    })), null, 2);
    log(`任务 ${data.items.length} 条`, "ok");
  } catch (e) { log(`任务列表失败：${e}`, "err"); }
}

async function importNotice() {
  if (!projectId) return log("先选用项目", "err");
  const url = $("n-url").value.trim();
  if (!url) return log("请输入公告 URL", "err");
  try {
    const data = await api(`/projects/${projectId}/tender-notices/import-url`, { method: "POST", body: { url } });
    $("m-extra").textContent = JSON.stringify(data, null, 2);
    log(`公告导入：status=${data.status}${data.error_code ? " 错误=" + data.error_code : " file_id=" + data.file_id}`, data.status === 2 ? "ok" : "err");
    refreshFiles();
  } catch (e) { log(`公告导入失败：${e}`, "err"); }
}

async function loadNotices() {
  if (!projectId) return log("先选用项目", "err");
  try {
    const data = await api(`/projects/${projectId}/tender-notices`);
    $("m-extra").textContent = JSON.stringify(data.items, null, 2);
    log(`公告导入记录 ${data.items.length} 条`, "ok");
  } catch (e) { log(`公告列表失败：${e}`, "err"); }
}

/* ---------- 资料 ---------- */
function panelMaterial() {
  $("panel").innerHTML = `
    <h3>资料上传与解析</h3>
    <div class="row">
      <select id="m-target"><option value="project">项目材料</option><option value="enterprise">企业资料</option></select>
      <input type="file" id="m-file">
      <button onclick="uploadFile()">上传</button>
      <button class="ghost" onclick="ingestAssets()">企业资料导入分类</button>
      <button class="ghost" onclick="loadIngestQueue()">处理队列</button>
    </div>
    <div class="row"><button class="ghost" onclick="parseProject()">触发招标解析任务</button>
      <span class="muted">当前项目：#${projectId ?? "未选"}</span></div>
    <div class="row"><input id="n-url" placeholder="招标公告 URL（安全导入，SSRF 防护）">
      <button onclick="importNotice()">导入公告</button>
      <button class="ghost" onclick="loadNotices()">公告列表</button></div>
    <h4>文件</h4><table><thead><tr><th>ID</th><th>名称</th><th>归属</th><th>状态</th><th>解析</th></tr></thead><tbody id="m-files"></tbody></table>
    <h4>企业资料</h4><table><thead><tr><th>ID</th><th>名称</th><th>类型</th><th>状态</th><th>操作</th></tr></thead><tbody id="m-assets"></tbody></table>
    <pre id="m-extra" class="muted"></pre>`;
  refreshFiles(); refreshAssets();
}

async function uploadFile() {
  const file = $("m-file").files[0];
  if (!file) return log("请选择文件", "err");
  const target = $("m-target").value;
  if (target === "project" && !projectId) return log("上传项目材料前请先在“项目”页选用项目（Issue #10 P1.11）", "err");
  const fd = new FormData();
  fd.append("target", target);
  if (target === "project") fd.append("project_id", String(projectId));
  fd.append("files", file);
  try {
    const data = await api("/files/upload", { method: "POST", body: fd });
    log(`上传 ${file.name} → ${JSON.stringify(data.files[0])}`, "ok");
    refreshFiles(); refreshAssets();
  } catch (e) { log(`上传失败：${e}`, "err"); }
}

async function refreshFiles() {
  try {
    const data = await api("/files?size=50");
    $("m-files").innerHTML = data.items.map((f) => `
      <tr><td>${f.file_id}</td><td>${esc(f.name)}</td><td>${f.project_id ? "项目" : "企业"}</td><td>${f.status}</td>
      <td><button class="ghost" onclick="authedDownload('/files/${f.file_id}/download','文件')">下载</button> ·
          <button class="ghost" onclick="viewBlocks(${f.file_id})">文本块</button></td></tr>`).join("");
  } catch (e) { log(`文件列表失败：${e}`, "err"); }
}

async function viewBlocks(id) {
  try {
    const data = await api(`/files/${id}/blocks`);
    log("文本块：" + data.items.map((b) => b.text).join(" | ").slice(0, 300), "ok");
  } catch (e) { log(`文本块失败：${e}`, "err"); }
}

async function refreshAssets() {
  try {
    const data = await api("/enterprise/assets");
    $("m-assets").innerHTML = data.map((a) => `
      <tr><td>${a.asset_id}</td><td>${esc(a.name)}</td><td>${esc(a.asset_type)}</td><td>${a.status}</td>
      <td><button class="ghost" onclick="listFacts(${a.asset_id})">facts</button>
          <button class="ghost" onclick="listAssetRevisions(${a.asset_id})">revisions</button></td></tr>`).join("");
  } catch { $("m-assets").innerHTML = "<tr><td colspan=4>未登录或无权限</td></tr>"; }
}

async function listFacts(assetId) {
  try {
    const data = await api(`/enterprise/assets/${assetId}/facts`);
    $("m-extra").innerHTML = data.items.map((f) => `
      <div style="border:1px solid #ccc;margin:4px 0;padding:4px">
        fact#${f.fact_id} ${esc(f.fact_key)} = ${esc(JSON.stringify(f.fact_value))} (status ${f.status})
        <button class="ghost" onclick="confirmFact(${f.fact_id})">确认</button>
        <input id="fix-${f.fact_id}" placeholder="纠正值"><button class="ghost" onclick="correctFact(${f.fact_id})">纠正</button>
      </div>`).join("") || "无事实";
  } catch (e) { log(`facts 失败：${e}`, "err"); }
}

async function confirmFact(factId) {
  try {
    const r = await api(`/enterprise/facts/${factId}`, { method: "PUT", body: { confirmed: true } });
    log(`事实 ${factId} 已确认（修订 #${r.revision_no}）`, "ok");
  } catch (e) { log(`确认失败：${e}`, "err"); }
}

async function correctFact(factId) {
  const val = $(`fix-${factId}`).value.trim();
  if (!val) return log("请输入纠正值", "err");
  try {
    const r = await api(`/enterprise/facts/${factId}`, { method: "PUT", body: { fact_value: val, note: "demo 纠正" } });
    log(`事实 ${factId} 已纠正为 ${esc(JSON.stringify(r.fact_value))}（修订 #${r.revision_no}）`, "ok");
  } catch (e) { log(`纠正失败：${e}`, "err"); }
}

async function listAssetRevisions(assetId) {
  try {
    const data = await api(`/enterprise/assets/${assetId}/revisions`);
    $("m-extra").textContent = JSON.stringify(data.items, null, 2);
  } catch (e) { log(`revisions 失败：${e}`, "err"); }
}

async function ingestAssets() {
  try {
    const assets = await api("/enterprise/assets");
    const ids = assets.map((a) => a.asset_id);
    if (!ids.length) return log("企业资料为空", "err");
    const data = await api("/enterprise/ingest", { method: "POST", body: { asset_ids: ids } });
    log(`导入分类：${JSON.stringify(data.classified)}`, "ok");
    refreshAssets();
  } catch (e) { log(`导入失败：${e}`, "err"); }
}

async function loadIngestQueue() {
  try {
    const data = await api("/enterprise/ingest");
    $("m-extra").textContent = JSON.stringify(data.items, null, 2);
    log(`处理队列 ${data.items.length} 条`, "ok");
  } catch (e) { log(`处理队列失败：${e}`, "err"); }
}

async function parseProject() {
  if (!projectId) return log("请先选用项目", "err");
  try {
    const files = await api(`/files?target=project&project_id=${projectId}`);
    const ids = files.items.map((f) => f.file_id);
    if (!ids.length) return log("项目无文件", "err");
    const t = await api(`/projects/${projectId}/tasks`, { method: "POST", body: { task_type: "tender_parse", payload: { file_ids: ids }, idempotency_key: `parse-${Date.now()}` } });
    pollTask(t.task_id);
  } catch (e) { log(`任务提交失败：${e}`, "err"); }
}

/* ---------- 要求/匹配（Issue #10 P1.13：Requirement 确认/修正 + 资料匹配完整闭环） ---------- */
function panelRequirements() {
  $("panel").innerHTML = `
    <h3>招标要求（Requirement）管理</h3>
    <div class="row">
      <select id="r-type">
        <option value="qualification">资格要求</option><option value="tech_requirement">技术要求</option>
        <option value="quote_rule">报价规则</option><option value="basic_info">基本信息</option><option value="other">其他</option>
      </select>
      <input id="r-content" placeholder="要求内容">
      <button onclick="upsertRequirement()">新增要求（upsert）</button>
      <button class="ghost" onclick="matchMaterials()">发起资料匹配</button>
      <span class="muted">当前项目：#${projectId ?? "未选"}</span>
    </div>
    <table><thead><tr><th>ID</th><th>类型</th><th>内容</th><th>版本</th><th>确认状态</th><th>操作</th></tr></thead><tbody id="r-rows"></tbody></table>
    <div class="row"><input id="r-correct" placeholder="修正后的内容（先选中要修正的行）"><button class="ghost" onclick="correctSelected()">修正选中</button></div>
    <pre id="r-extra" class="muted"></pre>`;
  loadRequirements();
}

let selectedReq = null;
async function loadRequirements() {
  if (!projectId) return log("先选用项目", "err");
  try {
    const rows = await api(`/requirements?project_id=${projectId}`);
    $("r-rows").innerHTML = rows.map((r) => `
      <tr data-id="${r.req_id}" data-rev="${r.revision}">
        <td>${r.req_id}</td><td>${esc(r.req_type)}</td><td>${esc(r.content)}</td><td>r${r.revision}</td>
        <td>${esc(r.confirm_status)}</td>
        <td><button class="r-confirm" data-id="${r.req_id}" data-rev="${r.revision}">确认</button>
            <button class="ghost r-reject" data-id="${r.req_id}" data-rev="${r.revision}">拒绝</button>
            <button class="ghost r-pick" data-id="${r.req_id}" data-rev="${r.revision}">选中修正</button></td>
      </tr>`).join("") || "<tr><td colspan='6'>暂无要求（先上传材料并触发“招标解析”）</td></tr>";
    document.querySelectorAll("#r-rows .r-confirm").forEach((b) => {
      b.onclick = () => confirmRequirement(Number(b.dataset.id), Number(b.dataset.rev), true);
    });
    document.querySelectorAll("#r-rows .r-reject").forEach((b) => {
      b.onclick = () => confirmRequirement(Number(b.dataset.id), Number(b.dataset.rev), false);
    });
    document.querySelectorAll("#r-rows .r-pick").forEach((b) => {
      b.onclick = () => {
        selectedReq = { id: Number(b.dataset.id), rev: Number(b.dataset.rev) };
        log(`已选中要求 #${selectedReq.id}（r${selectedReq.rev}）待修正`, "ok");
      };
    });
    log(`已加载要求 ${rows.length} 条`, "ok");
  } catch (e) { log(`要求列表失败：${e}`, "err"); }
}

async function upsertRequirement() {
  if (!projectId) return log("先选用项目", "err");
  const content = $("r-content").value.trim();
  if (!content) return log("要求内容不能为空", "err");
  try {
    const r = await api(`/projects/${projectId}/requirements/upsert`, {
      method: "POST",
      body: { requirements: [{ req_type: $("r-type").value, content, coordinates: [{ source: "manual" }] }] },
    });
    log(`已写入 ${r.count} 条要求`, "ok");
    loadRequirements();
  } catch (e) { log(`写入失败：${e}`, "err"); }
}

async function confirmRequirement(reqId, revision, confirmed) {
  try {
    const r = await api(`/projects/${projectId}/requirements/${reqId}/confirm`, {
      method: "PUT",
      body: { expected_revision: revision, confirmed },
    });
    log(`要求 #${reqId} 已${confirmed ? "确认" : "拒绝"}（${r.confirm_status}）`, "ok");
    loadRequirements();
  } catch (e) { log(`确认失败（注意 expected_revision CAS）：${e}`, "err"); }
}

async function correctSelected() {
  if (!selectedReq) return log("先在表格中“选中修正”一条要求", "err");
  const content = $("r-correct").value.trim();
  if (!content) return log("修正内容不能为空", "err");
  try {
    const r = await api(`/projects/${projectId}/requirements/${selectedReq.id}/correct`, {
      method: "PUT",
      body: { expected_revision: selectedReq.rev, content, coordinates: [{ source: "manual" }] },
    });
    log(`要求 #${selectedReq.id} 已修正 → r${r.revision}`, "ok");
    selectedReq = null;
    loadRequirements();
  } catch (e) { log(`修正失败：${e}`, "err"); }
}

async function matchMaterials() {
  if (!projectId) return log("先选用项目", "err");
  try {
    const t = await api(`/projects/${projectId}/tasks`, { method: "POST", body: { task_type: "material_match", payload: {}, idempotency_key: `mm-${Date.now()}` } });
    pollTask(t.task_id);
  } catch (e) { log(`资料匹配失败：${e}`, "err"); }
}

/* ---------- 成果 ---------- */
function panelDeliverable() {
  $("panel").innerHTML = `
    <h3>成果与版本</h3>
    <div class="row"><button onclick="createDeliverables()">创建三份成果</button>
      <button class="ghost" onclick="generateBid()">生成标书(bid_generate)</button>
      <button class="ghost" onclick="reviewBid()">校核(bid_review)</button></div>
    <div class="row"><textarea id="d-json" placeholder='{"nodes":[{"id":"n1","type":"paragraph","text":"内容"}]}'></textarea></div>
    <div class="row"><select id="d-sel"></select><button onclick="saveVersion()">保存新版本</button>
      <button class="ghost" onclick="aiEdit()">AI 修改选区</button></div>
    <div id="d-versions" class="muted"></div>
    <div class="row"><button class="ghost" onclick="createEditorSession()">创建编辑会话</button>
      <button class="ghost" onclick="saveCheckpoint()">保存检查点</button>
      <button class="ghost" onclick="completeEditorSession()">完成编辑</button>
      <button class="ghost" onclick="cancelEditorSession()">取消会话</button></div>
    <table><thead><tr><th>ID</th><th>类型</th><th>标题</th><th>当前版本</th><th>版本列表</th></tr></thead><tbody id="d-rows"></tbody></table>`;
  refreshDeliverables();
}

async function refreshDeliverables() {
  if (!$("d-rows")) return;  // 非成果页时跳过渲染
  if (!projectId) { $("d-rows").innerHTML = "<tr><td colspan=5>先选用项目</td></tr>"; return; }
  try {
    deliverables = await api(`/deliverables?project_id=${projectId}`);
    $("d-rows").innerHTML = deliverables.map((d) => `
      <tr><td>${d.deliverable_id}</td><td>${d.deliverable_type}</td><td>${esc(d.title)}</td><td>${d.current_version_no}</td>
      <td><button class="ghost" onclick="listVersions(${d.deliverable_id})">版本</button></td></tr>`).join("");
    $("d-sel").innerHTML = deliverables.map((d) => `<option value="${d.deliverable_id}">#${d.deliverable_id} ${esc(d.title)}</option>`).join("");
  } catch (e) { log(`成果列表失败：${e}`, "err"); }
}

async function createDeliverables() {
  if (!projectId) return log("先选用项目", "err");
  try {
    for (const [dtype, title, model] of [[1, "商务标", { nodes: [{ id: "n1", type: "paragraph", text: "商务响应" }] }],
      [2, "技术标", { nodes: [{ id: "n1", type: "paragraph", text: "技术方案" }] }],
      [3, "报价单", { type: "sheet", sheets: [{ name: "报价单", rows: [["材料", "价格"], ["电缆", "120"]] }] }]]) {
      const d = await api("/deliverables", { method: "POST", body: { project_id: projectId, deliverable_type: dtype, title } });
      await api(`/deliverables/${d.deliverable_id}/versions`, { method: "POST", body: { content: model, version_type: 2 } });
    }
    log("三份成果已创建", "ok");
    refreshDeliverables();
  } catch (e) { log(`创建成果失败：${e}`, "err"); }
}

async function saveVersion() {
  const id = $("d-sel").value;
  if (!id) return log("先创建成果", "err");
  try {
    const content = JSON.parse($("d-json").value || '{"nodes":[]}');
    const v = await api(`/deliverables/${id}/versions`, { method: "POST", body: { content, version_type: 4 } });
    log(`成果 ${id} 新版本 v${v.version_no}`, "ok");
    refreshDeliverables();
  } catch (e) { log(`保存失败：${e}`, "err"); }
}

async function listVersions(id) {
  try {
    const rows = await api(`/deliverables/${id}/versions`);
    $("d-versions").innerHTML = rows.map((v) =>
      `v${v.version_no}(type${v.version_type}) <a href="#" onclick="downloadVersion(${id},${v.version_no});return false">下载</a>`
    ).join(" · ");
    log(`成果 ${id} 版本：` + rows.map((v) => `v${v.version_no}(type${v.version_type})`).join(", "), "ok");
  } catch (e) { log(e, "err"); }
}

async function downloadVersion(id, no) {
  await authedDownload(`/deliverables/${id}/versions/${no}/download`, `deliverable_${id}_v${no}.docx`);
}

let editorSession = null;

async function createEditorSession() {
  const id = $("d-sel").value;
  if (!id) return log("先创建成果", "err");
  try {
    const s = await api(`/deliverables/${id}/editor-sessions`, { method: "POST", body: {} });
    editorSession = { id: s.session_id, lease: s.lease_token, base: s.base_version_no };
    $("d-json").value = JSON.stringify(s.content, null, 2);
    log(`编辑会话 #${s.session_id} 已创建（base v${s.base_version_no}）`, "ok");
  } catch (e) { log(`创建会话失败：${e}`, "err"); }
}

async function saveCheckpoint() {
  if (!editorSession) return log("先创建编辑会话", "err");
  const id = $("d-sel").value;
  try {
    const content = JSON.parse($("d-json").value || '{"nodes":[]}');
    await api(`/deliverables/${id}/editor-sessions/${editorSession.id}/checkpoint`, {
      method: "PUT",
      body: { lease_token: editorSession.lease, content },
    });
    log("检查点已保存", "ok");
  } catch (e) { log(`检查点失败：${e}`, "err"); }
}

async function completeEditorSession() {
  if (!editorSession) return log("先创建编辑会话", "err");
  const id = $("d-sel").value;
  try {
    const content = JSON.parse($("d-json").value || '{"nodes":[]}');
    const r = await api(`/deliverables/${id}/editor-sessions/${editorSession.id}/complete`, {
      method: "POST",
      body: { lease_token: editorSession.lease, content, expected_version_no: editorSession.base },
    });
    log(`编辑完成 → v${r.version_no}`, "ok");
    editorSession = null;
    refreshDeliverables();
  } catch (e) { log(`完成编辑失败：${e}`, "err"); }
}

async function cancelEditorSession() {
  if (!editorSession) return log("先创建编辑会话", "err");
  const id = $("d-sel").value;
  try {
    await api(`/deliverables/${id}/editor-sessions/${editorSession.id}/cancel`, {
      method: "POST",
      body: { lease_token: editorSession.lease },
    });
    log("会话已取消", "ok");
    editorSession = null;
  } catch (e) { log(`取消失败：${e}`, "err"); }
}

async function generateBid() {
  if (!projectId) return log("先选用项目", "err");
  try {
    const t = await api(`/projects/${projectId}/tasks`, { method: "POST", body: { task_type: "bid_generate", payload: { material_ref: "CABLE-YJV-3x95", cost: 100 }, idempotency_key: `bg-${Date.now()}` } });
    pollTask(t.task_id);
  } catch (e) { log(e, "err"); }
}

async function reviewBid() {
  if (!projectId) return log("先选用项目", "err");
  try {
    const t = await api(`/projects/${projectId}/tasks`, { method: "POST", body: { task_type: "bid_review", payload: {}, idempotency_key: `br-${Date.now()}` } });
    pollTask(t.task_id);
  } catch (e) { log(e, "err"); }
}

async function aiEdit() {
  const id = $("d-sel").value;
  if (!id) return log("先创建成果", "err");
  try {
    const d = await api(`/deliverables/${id}/content`);
    const node = d.model.nodes?.[0];
    if (!node) return log("成果无节点", "err");
    const diff = await api(`/deliverables/${id}/ai-edit`, { method: "POST", body: { selection: { type: "text", refs: [node.id] }, instruction: "本段为演示用修改文本" } });
    const applied = await api(`/deliverables/${id}/ai-edit/${diff.diff_id}/apply`, { method: "POST" });
    log(`AI 修改已应用 → v${applied.version_no}`, "ok");
    refreshDeliverables();
  } catch (e) { log(`AI 修改失败：${e}`, "err"); }
}

/* ---------- 任务/评标 ---------- */
function panelTask() {
  $("panel").innerHTML = `
    <h3>任务与模拟评标</h3>
    <div class="row">
      <button onclick="submitTask('tender_parse')">招标解析</button>
      <button onclick="submitTask('material_match')">资料匹配</button>
      <button onclick="submitTask('bid_generate')">生成标书</button>
      <button onclick="submitTask('bid_review')">校核</button>
      <button onclick="doEvaluate()">模拟评标</button>
    </div>
    <div class="row"><span class="muted">任务结果与评分会显示在下方日志；评标项：</span></div>
    <table><thead><tr><th>item_id</th><th>分类</th><th>问题</th><th>得分/满分</th><th>可提升</th><th>状态</th><th>建议</th></tr></thead><tbody id="t-items"></tbody></table>
    <div class="row"><input id="s-item-id" placeholder="item_id"><input id="s-suggestion" placeholder="修改后的建议">
      <button class="ghost" onclick="saveSuggestionOverride()">保存建议修改</button></div>
    <div class="row"><button class="ghost" onclick="confirmAll()">确认全部建议</button>
      <button class="ghost" onclick="reEvaluate()">重审受影响项</button></div>`;
}

async function saveSuggestionOverride() {
  const itemId = Number($("s-item-id").value);
  const suggestion = $("s-suggestion").value.trim();
  if (!itemId || !suggestion) return log("需要 item_id 与建议内容", "err");
  if (!scoreCtx) return log("先评标", "err");
  try {
    const r = await api(`/projects/${projectId}/scores/${scoreCtx.score_id}/items/${itemId}/suggestion`, {
      method: "PUT",
      body: { suggestion },
    });
    log(`建议已保存：item#${r.item_id} 生效建议=${esc(r.effective_suggestion)}`, "ok");
  } catch (e) { log(`保存建议失败：${e}`, "err"); }
}

async function submitTask(taskType) {
  if (!projectId) return log("先选用项目", "err");
  try {
    const t = await api(`/projects/${projectId}/tasks`, { method: "POST", body: { task_type: taskType, payload: {}, idempotency_key: `${taskType}-${Date.now()}` } });
    pollTask(t.task_id);
  } catch (e) { log(`${taskType} 提交失败：${e}`, "err"); }
}

let scoreCtx = null;
async function doEvaluate() {
  if (!projectId) return log("先选用项目", "err");
  try {
    const ev = await api(`/projects/${projectId}/evaluate`, { method: "POST", body: {} });
    scoreCtx = ev;
    log(`评标完成：总分 ${ev.total_score}，缺失 ${ev.missing_count}`, "ok");
    const items = await api(`/projects/${projectId}/scores/${ev.score_id}/items`);
    $("t-items").innerHTML = items.map((i) => `
      <tr><td>${i.item_id}</td><td>${esc(i.category)}</td><td>${esc(i.problem_description)}</td>
      <td>${i.got ?? "-"}/${i.full ?? "-"}</td><td>${i.improvable ?? "-"}</td><td>${i.status}</td>
      <td>${esc(i.effective_suggestion || "")}</td></tr>`).join("");
  } catch (e) { log(`评标失败：${e}`, "err"); }
}

async function confirmAll() {
  if (!scoreCtx) return log("先评标", "err");
  try {
    const items = await api(`/projects/${projectId}/scores/${scoreCtx.score_id}/items`);
    const r = await api(`/projects/${projectId}/scores/${scoreCtx.score_id}/items/confirm`, { method: "POST", body: { item_ids: items.map((i) => i.item_id), expected_version: scoreCtx.snapshot_id } });
    log(`确认结果：${JSON.stringify(r.results)}`, "ok");
  } catch (e) { log(`确认失败：${e}`, "err"); }
}

async function reEvaluate() {
  if (!scoreCtx) return log("先评标", "err");
  try {
    const items = await api(`/projects/${projectId}/scores/${scoreCtx.score_id}/items`);
    const r = await api(`/projects/${projectId}/re-evaluate`, { method: "POST", body: { item_ids: items.map((i) => i.item_id) } });
    log(`重审完成：总分 ${r.total_score}，提升 ${r.improved_count} 项`, "ok");
  } catch (e) { log(`重审失败：${e}`, "err"); }
}

async function pollTask(taskId) {
  /* Issue #10 P1.10/P1.12：优先 SSE 流（含进度与断线回退），持久化任务 id，刷新可恢复 */
  activeTaskId = taskId;
  localStorage.setItem(TASK_KEY(), String(taskId));
  log(`任务 ${taskId} 已提交，等待执行…`);
  try {
    const resp = await rawFetch(`/tasks/${taskId}/stream`);
    if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop() || "";
      for (const line of lines) {
        if (!line.startsWith("data:")) continue;
        let payload = null;
        try { payload = JSON.parse(line.slice(5).trim()); } catch { continue; }
        const p = payload.progress || {};
        const status = payload.status;
        if (p.phase) log(`任务 ${taskId}：${p.phase} ${p.status || ""} ${p.percent != null ? p.percent + "%" : ""} ${p.current_work || ""}`);
        if (status === 3 || status === "done") {
          log(`任务 ${taskId} 完成：${JSON.stringify(payload.result).slice(0, 240)}`, "ok");
          localStorage.removeItem(TASK_KEY());
          activeTaskId = null;
          refreshDeliverables(); refreshProjects();
          return;
        }
        if (status === 4 || status === 6 || status === "failed" || status === "cancelled") {
          log(`任务 ${taskId} 终态：${status}`, "err");
          localStorage.removeItem(TASK_KEY());
          activeTaskId = null;
          return;
        }
      }
    }
  } catch (e) {
    log(`SSE 中断（${e}），回退轮询`, "err");
  }
  // 轮询回退：长任务预算 300s，不再 30s 误报超时
  for (let i = 0; i < 300; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    try {
      const st = await api(`/tasks/${taskId}`);
      if (st.status === 3) {
        log(`任务 ${taskId} 完成：${JSON.stringify(st.result).slice(0, 240)}`, "ok");
        localStorage.removeItem(TASK_KEY()); activeTaskId = null;
        refreshDeliverables(); refreshProjects();
        return;
      }
      if (st.status === 6 || st.status === 4) {
        log(`任务 ${taskId} 终态：${st.status}`, "err");
        localStorage.removeItem(TASK_KEY()); activeTaskId = null;
        return;
      }
    } catch { /* 网络抖动继续 */ }
  }
  log(`任务 ${taskId} 仍在执行（超 5 分钟），可在“任务/评标”页查看状态；刷新页面会自动恢复跟踪`, "err");
}

/* ---------- 报价 ---------- */
function panelQuote() {
  $("panel").innerHTML = `
    <h3>报价</h3>
    <div class="row">
      <input id="q-material" value="CABLE-YJV-3x95">
      <input id="q-cost" type="number" value="100">
      <button onclick="calcQuote()">测算</button>
      <button class="ghost" onclick="strategy('win')">中标策略</button>
      <button class="ghost" onclick="aiSuggest()">AI 参考价</button>
      <button class="ghost" onclick="applyQuote()">应用到报价单</button>
    </div>
    <div class="row"><button class="ghost" onclick="loadCalcHistory()">历史测算</button>
      <button class="ghost" onclick="loadTrend()">样本趋势</button></div>
    <pre id="q-result" class="muted"></pre>`;
}

async function calcQuote() {
  if (!projectId) return log("先选用项目（报价与项目绑定）", "err");
  try {
    const data = await api("/quotes/calculate", { method: "POST", body: { material_ref: $("q-material").value, cost: Number($("q-cost").value), min_profit_rate: 0.1, project_id: projectId } });
    calcId = data.calc_id;
    $("q-result").textContent = JSON.stringify(data.result, null, 2);
    log(`测算完成 calc#${calcId} 建议价 ${data.result.suggested}`, "ok");
  } catch (e) { $("q-result").textContent = String(e); log(`测算失败：${e}`, "err"); }
}

async function strategy(name) {
  if (!calcId) return log("先测算", "err");
  try {
    const data = await api("/quotes/strategies", { method: "POST", body: { calc_id: calcId, strategy: name } });
    $("q-result").textContent = JSON.stringify(data, null, 2);
    log(`策略 ${name}：${data.suggested_price}`, "ok");
  } catch (e) { log(`策略失败：${e}`, "err"); }
}

async function loadCalcHistory() {
  if (!projectId) return log("先选用项目", "err");
  try {
    const data = await api(`/quotes?project_id=${projectId}`);
    $("q-result").textContent = JSON.stringify(data.items.map((c) => ({
      calc_id: c.calc_id,
      status: c.status,
      suggested: c.result && c.result.suggested,
      applied_version_no: c.applied_version_no,
      sample_count: c.sample_count,
      created_at: c.created_at,
    })), null, 2);
    log(`测算记录 ${data.items.length} 条`, "ok");
  } catch (e) { log(`历史测算失败：${e}`, "err"); }
}

async function loadTrend() {
  try {
    const ref = $("q-material").value || "CABLE-YJV-3x95";
    const data = await api(`/quotes/history/${encodeURIComponent(ref)}/trend`);
    $("q-result").textContent = JSON.stringify(data, null, 2);
    log(`样本趋势：${data.sample_count} 条，中位数 ${data.median_price}`, "ok");
  } catch (e) { log(`趋势失败：${e}`, "err"); }
}

async function aiSuggest() {
  if (!calcId) return log("先测算", "err");
  try {
    const data = await api("/quotes/ai-suggest", { method: "POST", body: { calc_id: calcId, basis: "华东区中标样本（演示）" } });
    $("q-result").textContent = JSON.stringify(data, null, 2);
    if (data.unavailable) {
      log(`AI 报价建议：${data.message}`, "err");  // 方案 2（Issue #6）：无依据不出任何数字
    } else {
      log(`AI 参考区间（策略建议，非最终报价）：${JSON.stringify(data.price_range)}`, "ok");
    }
  } catch (e) { log(`AI 建议失败：${e}`, "err"); }
}

async function applyQuote() {
  if (!calcId) return log("先测算", "err");
  if (!projectId) return log("先选用项目", "err");
  try {
    const list = await api(`/deliverables?project_id=${projectId}`);
    deliverables = list;
    const quote = list.find((d) => d.deliverable_type === 3);
    if (!quote) return log("没有报价单成果（先执行“生成标书”）", "err");
    const data = await api("/quotes/apply", { method: "POST", body: { calc_id: calcId, deliverable_id: quote.deliverable_id, expected_version_no: quote.current_version_no } });
    log(`报价已应用 → 报价单 v${data.new_version_no}`, "ok");
    refreshDeliverables();
  } catch (e) { log(`应用失败：${e}`, "err"); }
}

/* ---------- 导出 ---------- */
function panelExport() {
  $("panel").innerHTML = `
    <h3>终检与导出</h3>
    <div class="row">
      <button onclick="finalCheck()">终稿检查</button>
      <button onclick="doExport()">导出 DOCX/XLSX</button>
      <button class="ghost" onclick="downloadDeliveryPackage()">下载交付包</button>
    </div>
    <pre id="e-result" class="muted"></pre>`;
}

async function downloadDeliveryPackage() {
  if (!projectId) return log("先选用项目", "err");
  await authedDownload(`/projects/${projectId}/delivery-package`, `交付包_project_${projectId}.zip`);
}

async function finalCheck() {
  if (!projectId) return log("先选用项目", "err");
  try {
    const data = await api(`/projects/${projectId}/check`, { method: "POST", body: {} });
    $("e-result").textContent = JSON.stringify(data, null, 2);
    log(`终检 ${data.passed ? "通过" : "未通过"}`, data.passed ? "ok" : "err");
  } catch (e) { log(`终检失败：${e}`, "err"); }
}

async function doExport() {
  if (!projectId) return log("先选用项目", "err");
  try {
    const data = await api(`/projects/${projectId}/export`, { method: "POST", body: { formats: ["docx", "xlsx"], with_manifest: true } });
    $("e-result").textContent = JSON.stringify(data, null, 2);
    log(`导出完成：${data.files.length} 个文件（可在下方“下载交付包”获取）`, "ok");
  } catch (e) { log(`导出失败：${e}`, "err"); }
}

/* ---------- 搜索/对话 ---------- */
function panelSearch() {
  $("panel").innerHTML = `
    <h3>搜索与对话</h3>
    <div class="row"><input id="s-query" placeholder="搜索关键词"><button onclick="doSearch()">搜索</button></div>
    <pre id="s-result" class="muted"></pre>
    <div class="row"><input id="k-query" placeholder="企业知识检索（历史案例/资料）"><button onclick="doKnowledge()">知识检索</button></div>
    <pre id="k-result" class="muted"></pre>
    <div class="row"><select id="c-sel"></select><button onclick="newConversation()">新建会话</button></div>
    <div class="row"><input id="c-msg" placeholder="向助手提问"><button onclick="sendMessage()">发送</button></div>
    <pre id="c-history" class="muted"></pre>`;
  loadConversations();
}

async function doKnowledge() {
  try {
    const data = await api("/knowledge/search", { method: "POST", body: { query: $("k-query").value, project_id: projectId } });
    $("k-result").textContent = JSON.stringify(data, null, 2);
    log(`知识检索命中 ${data.items.length} 条（来源可追溯）`, "ok");
  } catch (e) { $("k-result").textContent = String(e); log(`知识检索失败：${e}`, "err"); }
}

async function doSearch() {
  try {
    const data = await api("/searches", { method: "POST", body: { query: $("s-query").value } });
    $("s-result").textContent = JSON.stringify(data, null, 2);
    log(`搜索返回 ${data.results.length} 条`, "ok");
  } catch (e) { $("s-result").textContent = String(e); log(`搜索失败：${e}`, "err"); }
}

async function loadConversations() {
  if (!projectId) { $("c-sel").innerHTML = "<option>先选用项目</option>"; return; }
  try {
    const data = await api(`/projects/${projectId}/conversations`);
    $("c-sel").innerHTML = data.items.map((c) =>
      `<option value="${c.conversation_id}">#${c.conversation_id} ${esc(c.title)}</option>`).join("") || "<option>暂无会话</option>";
    if (data.items.length) {
      $("c-sel").value = String(data.items[data.items.length - 1].conversation_id);
      showMessages();
    }
  } catch (e) { log(`会话列表失败：${e}`, "err"); }
}

async function newConversation() {
  if (!projectId) return log("先选用项目", "err");
  try {
    const c = await api(`/projects/${projectId}/conversations`, { method: "POST", body: {} });
    log(`已创建会话 #${c.conversation_id}`, "ok");
    await loadConversations();
    $("c-sel").value = String(c.conversation_id);  // 新建后立即选中，用户可直接提问（公网修复：原实现未等待列表加载，发送被静默拦截）
    showMessages();
  } catch (e) { log(`创建会话失败：${e}`, "err"); }
}

async function showMessages() {
  const cid = $("c-sel").value;
  if (!/^\d+$/.test(cid) || !projectId) return;  // 占位选项不作为真实会话 ID（Issue #10 P1.11）
  try {
    const data = await api(`/projects/${projectId}/conversations/${cid}/messages`);
    $("c-history").textContent = data.items.map((m) =>
      `${m.role === "user" ? "用户" : "助手"}: ${m.content}`).join("\n\n");
  } catch (e) { log(`消息列表失败：${e}`, "err"); }
}

async function sendMessage() {
  const cid = $("c-sel").value;
  const msg = $("c-msg").value.trim();
  if (!projectId) return log("先选用项目", "err");
  if (!/^\d+$/.test(cid)) return log("先新建会话（当前没有可用会话）", "err");
  if (!msg) return log("消息为空", "err");
  try {
    const data = await api(`/projects/${projectId}/conversations/${cid}/messages`, {
      method: "POST",
      body: { message: msg },
    });
    $("c-msg").value = "";
    log(`助手（${data.mode}）：${data.reply}`, "ok");
    showMessages();
  } catch (e) { log(`发送失败：${e}`, "err"); }
}

/* ---------- 设置 / 连接测试（Issue #5 测试客户端） ---------- */
function loadEnvs() {
  try { return JSON.parse(localStorage.getItem("bidvolt_envs") || "[]"); } catch { return []; }
}
function saveEnvs(envs) { localStorage.setItem("bidvolt_envs", JSON.stringify(envs)); }
function currentEnv() {
  try { return JSON.parse(localStorage.getItem("bidvolt_env_cur") || "null"); } catch { return null; }
}

function panelSettings() {
  const envs = loadEnvs();
  const cur = currentEnv();
  $("panel").innerHTML = `
    <h3>服务地址与连接测试</h3>
    <div class="row">
      <input id="env-name" placeholder="环境名（如 本地/测试/生产）">
      <input id="env-base" placeholder="后端地址（如 http://127.0.0.1:8123，留空=同源）">
      <button onclick="addEnv()">保存环境</button>
    </div>
    <table><thead><tr><th>环境名</th><th>地址</th><th>当前</th><th>操作</th></tr></thead><tbody>
      ${envs.map((e, i) => `<tr>
        <td>${esc(e.name)}</td><td>${esc(e.base || "（同源）")}</td>
        <td>${cur && cur.name === e.name ? "✔" : ""}</td>
        <td><button class="ghost" onclick="selectEnv(${i})">选用</button>
            <button class="ghost" onclick="deleteEnv(${i})">删除</button></td>
      </tr>`).join("")}
    </tbody></table>
    <div class="row">
      <button onclick="testConnection()">测试连接（healthz + openapi）</button>
      <span class="muted">切换环境会退出登录并清理业务上下文（凭据按环境隔离，不跨地址发送）</span>
    </div>
    <pre id="env-result" class="muted">当前：${cur ? `${cur.name} → ${cur.base || "同源"}` : "同源默认（未保存环境）"}</pre>`;
}

function _switchEnv(name, base) {
  /* 切换环境：仅当地址真正变化时清除凭据并退出登录（Issue #10 P0.3）；
     同地址保存/切换（如 E2E 保存当前环境）保持会话。 */
  const changed = base !== API_BASE;
  if (changed) {
    token = "";
    localStorage.removeItem(TOKEN_KEY());
    localStorage.removeItem(REFRESH_KEY());
    localStorage.removeItem(USER_KEY());
    clearBusinessContext();
  }
  localStorage.setItem("bidvolt_env_cur", JSON.stringify({ name, base }));
  API_BASE = base;
  renderAuth();
}

function _validBase(base) {
  /* 仅允许 http/https，禁止其他协议（Issue #10 P0.3） */
  return /^https?:\/\/.+/i.test(base);
}

function addEnv() {
  const name = $("env-name").value.trim();
  const base = $("env-base").value.trim().replace(/\/+$/, "");
  if (!name) return log("环境名不能为空", "err");
  if (base && !_validBase(base)) return log("地址必须以 http:// 或 https:// 开头", "err");
  const envs = loadEnvs().filter((e) => e.name !== name);
  envs.push({ name, base });
  saveEnvs(envs);
  _switchEnv(name, base);
  log(`已保存并切换环境：${name} → ${base || "同源"}（已退出登录，请重新登录）`, "ok");
  panelSettings();
}

function selectEnv(i) {
  const envs = loadEnvs();
  const env = envs[i];
  if (!env) return;
  _switchEnv(env.name, env.base);
  log(`已切换环境：${env.name} → ${env.base || "同源"}（已退出登录，请重新登录）`, "ok");
  panelSettings();
}

function deleteEnv(i) {
  const envs = loadEnvs();
  const removed = envs.splice(i, 1);
  saveEnvs(envs);
  const cur = currentEnv();
  if (cur && removed[0] && cur.name === removed[0].name) {
    localStorage.removeItem("bidvolt_env_cur");
    API_BASE = "";
    token = "";
    localStorage.removeItem(TOKEN_KEY());
    localStorage.removeItem(REFRESH_KEY());
    localStorage.removeItem(USER_KEY());
    clearBusinessContext();
    renderAuth();
  }
  panelSettings();
}

async function testConnection() {
  const base = API_BASE;
  const out = [];
  let allOk = true;
  for (const [label, path] of [["healthz", "/healthz"], ["openapi", "/openapi.json"]]) {
    const url = base + path;
    const t0 = Date.now();
    try {
      const resp = await fetch(url, { method: "GET" });
      const text = await resp.text();
      out.push(`${label}: HTTP ${resp.status}（${Date.now() - t0}ms）${text.slice(0, 80)}`);
      if (resp.status !== 200) allOk = false;
    } catch (e) {
      out.push(`${label}: 失败 — ${String(e)}`);
      allOk = false;
    }
  }
  $("env-result").textContent = `测试目标：${base || "同源（本页）"}\n` + out.join("\n");
  log(`连接测试：${allOk ? "全部通过" : "存在失败项"}`, allOk ? "ok" : "err");  // 两项都 200 才算通过（Issue #10 P2）
}

/* 供 E2E/调试读取当前会话（不暴露 refresh token） */
window.getBidvoltToken = () => token;

/* ---------- 初始化（Issue #10 P1.12：刷新恢复项目/任务上下文，并校验会话有效性） ---------- */
async function initApp() {
  renderTabs();
  renderAuth();
  if (!token) {
    renderPanel("auth");
    return;
  }
  // 校验会话是否仍有效（过期 token 不显示“已登录”）
  try {
    const meData = await api("/auth/me");
    localStorage.setItem(USER_KEY(), String(meData.user_id));
  } catch {
    token = "";
    localStorage.removeItem(TOKEN_KEY());
    localStorage.removeItem(REFRESH_KEY());
    renderAuth();
    renderPanel("auth");
    return;
  }
  const savedProject = localStorage.getItem(PROJECT_KEY());
  if (savedProject) {
    projectId = Number(savedProject);
    const savedTask = localStorage.getItem(TASK_KEY());
    renderPanel("project");
    log(`已恢复项目 #${projectId}（刷新恢复）`, "ok");
    if (savedTask) pollTask(Number(savedTask));
  } else {
    renderPanel("project");
  }
}

initApp();
