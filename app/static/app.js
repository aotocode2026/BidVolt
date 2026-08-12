/* BidVolt Demo：直接调用 /api/v1，覆盖全业务流程 */
const API = "/api/v1";
let token = localStorage.getItem("bidvolt_token") || "";
let projectId = null;
let deliverables = [];

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function log(msg, cls = "") {
  const pre = $("log");
  const div = document.createElement("div");
  div.className = cls;
  div.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  pre.prepend(div);
}

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (opts.body && !(opts.body instanceof FormData)) headers["Content-Type"] = "application/json";
  const resp = await fetch(API + path, { ...opts, headers, body: opts.body instanceof FormData ? opts.body : opts.body ? JSON.stringify(opts.body) : undefined });
  if (resp.status === 204) return null;
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!resp.ok) throw new Error(data?.detail || data || `HTTP ${resp.status}`);
  return data;
}

function saveAuth(data) {
  token = data.access_token;
  localStorage.setItem("bidvolt_token", token);
  localStorage.setItem("bidvolt_refresh", data.refresh_token);
  renderAuth();
}

function renderAuth() {
  $("authbar").innerHTML = token
    ? `<span>已登录 user#${localStorage.getItem("bidvolt_user") || ""}</span> <button class="ghost" onclick="logout()">退出</button>`
    : "<span>未登录</span>";
}

async function logout() {
  try { await api("/auth/logout", { method: "POST", body: { refresh_token: localStorage.getItem("bidvolt_refresh") } }); } catch {}
  token = ""; localStorage.clear(); renderAuth(); renderPanel("auth");
}

const TABS = [
  ["auth", "认证"], ["project", "项目"], ["material", "资料"], ["deliverable", "成果"],
  ["task", "任务/评标"], ["quote", "报价"], ["export", "导出"], ["search", "搜索/对话"],
];

function renderTabs() {
  $("tabs").innerHTML = TABS.map(([id, label]) => `<button data-tab="${id}">${label}</button>`).join("");
  document.querySelectorAll("#tabs button").forEach((b) => b.onclick = () => renderPanel(b.dataset.tab));
}

function renderPanel(tab) {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  const fn = { auth: panelAuth, project: panelProject, material: panelMaterial, deliverable: panelDeliverable, task: panelTask, quote: panelQuote, export: panelExport, search: panelSearch }[tab];
  fn();
}

/* ---------- 认证 ---------- */
function panelAuth() {
  $("panel").innerHTML = `
    <h3>注册 / 登录</h3>
    <div class="row"><input id="a-email" placeholder="邮箱"><input id="a-pwd" type="password" placeholder="密码(含字母数字)"><input id="a-name" placeholder="企业名称"></div>
    <div class="row">
      <button onclick="doAuth('register')">注册</button>
      <button class="ghost" onclick="doAuth('login')">登录</button>
      <button class="ghost" onclick="me()">查看 /auth/me</button>
    </div>
    <pre id="a-result" class="muted"></pre>`;
}

async function doAuth(mode) {
  const body = mode === "register"
    ? { email: $("a-email").value, password: $("a-pwd").value, enterprise_name: $("a-name").value }
    : { email: $("a-email").value, password: $("a-pwd").value };
  try {
    const data = await api(`/auth/${mode}`, { method: "POST", body });
    saveAuth(data);
    localStorage.setItem("bidvolt_user", data.user_id);
    $("a-result").textContent = JSON.stringify(data, null, 2);
    log(`${mode} 成功`, "ok");
  } catch (e) { $("a-result").textContent = String(e); log(`认证失败：${e}`, "err"); }
}

async function me() {
  try { $("a-result").textContent = JSON.stringify(await api("/auth/me"), null, 2); } catch (e) { $("a-result").textContent = String(e); }
}

/* ---------- 项目 ---------- */
function panelProject() {
  $("panel").innerHTML = `
    <h3>项目</h3>
    <div class="row"><input id="p-name" placeholder="项目名称"><input id="p-no" placeholder="招标编号(可选)">
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
    $("p-rows").innerHTML = data.items.map((p) => `
      <tr><td>${p.project_id}</td><td>${esc(p.name)}</td><td>${esc(p.tender_no || "")}</td><td>${p.status}</td>
      <td><button onclick="selectProject(${p.project_id},'${esc(p.name)}')">选用</button>
          <button class="ghost" onclick="archiveProject(${p.project_id})">归档</button></td></tr>`).join("");
  } catch (e) { log(`项目列表失败：${e}`, "err"); }
}

async function createProject() {
  try {
    const p = await api("/projects", { method: "POST", body: { name: $("p-name").value, tender_no: $("p-no").value || null } });
    selectProject(p.project_id, p.name);
    log(`项目创建成功 #${p.project_id}`, "ok");
  } catch (e) { log(`创建失败：${e}`, "err"); }
}

function selectProject(id, name) {
  projectId = id;
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

/* ---------- 资料 ---------- */
function panelMaterial() {
  $("panel").innerHTML = `
    <h3>资料上传与解析</h3>
    <div class="row">
      <select id="m-target"><option value="project">项目材料</option><option value="enterprise">企业资料</option></select>
      <input type="file" id="m-file">
      <button onclick="uploadFile()">上传</button>
      <button class="ghost" onclick="ingestAssets()">企业资料导入分类</button>
    </div>
    <div class="row"><button class="ghost" onclick="parseProject()">触发招标解析任务</button>
      <span class="muted">当前项目：#${projectId ?? "未选"}</span></div>
    <h4>文件</h4><table><thead><tr><th>ID</th><th>名称</th><th>归属</th><th>状态</th><th>解析</th></tr></thead><tbody id="m-files"></tbody></table>
    <h4>企业资料</h4><table><thead><tr><th>ID</th><th>名称</th><th>类型</th><th>状态</th></tr></thead><tbody id="m-assets"></tbody></table>`;
  refreshFiles(); refreshAssets();
}

async function uploadFile() {
  const file = $("m-file").files[0];
  if (!file) return log("请选择文件", "err");
  const target = $("m-target").value;
  const fd = new FormData();
  fd.append("target", target);
  if (target === "project" && projectId) fd.append("project_id", String(projectId));
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
      <td><a href="/api/v1/files/${f.file_id}/download" download>下载</a> ·
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
    $("m-assets").innerHTML = data.map((a) => `<tr><td>${a.asset_id}</td><td>${esc(a.name)}</td><td>${esc(a.asset_type)}</td><td>${a.status}</td></tr>`).join("");
  } catch { $("m-assets").innerHTML = "<tr><td colspan=4>未登录或无权限</td></tr>"; }
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
  try {
    const resp = await fetch(`${API}/deliverables/${id}/versions/${no}/download`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const blob = await resp.blob();
    const cd = resp.headers.get("content-disposition") || "";
    const m = cd.match(/filename\*=UTF-8''(.+)/);
    const fname = m ? decodeURIComponent(m[1]) : `deliverable_${id}_v${no}.docx`;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = fname;
    document.body.appendChild(a);
    a.click();
    a.remove();
    log(`已下载 ${fname}`, "ok");
  } catch (e) { log(`下载失败：${e}`, "err"); }
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
    <table><thead><tr><th>item_id</th><th>分类</th><th>问题</th><th>得分/满分</th><th>可提升</th><th>状态</th></tr></thead><tbody id="t-items"></tbody></table>
    <div class="row"><button class="ghost" onclick="confirmAll()">确认全部建议</button>
      <button class="ghost" onclick="reEvaluate()">重审受影响项</button></div>`;
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
      <td>${i.got ?? "-"}/${i.full ?? "-"}</td><td>${i.improvable ?? "-"}</td><td>${i.status}</td></tr>`).join("");
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
  log(`任务 ${taskId} 已提交，轮询中…`);
  for (let i = 0; i < 30; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    try {
      const st = await api(`/tasks/${taskId}`);
      if (st.status === 3) { log(`任务 ${taskId} 完成：${JSON.stringify(st.result).slice(0, 200)}`, "ok"); refreshDeliverables(); return; }
      if (st.status === 6) { log(`任务 ${taskId} 终态失败：${JSON.stringify(st.error)}`, "err"); return; }
    } catch { /* 继续轮询 */ }
  }
  log(`任务 ${taskId} 轮询超时`);
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
    <pre id="q-result" class="muted"></pre>`;
}

let calcId = null;
async function calcQuote() {
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

async function aiSuggest() {
  if (!calcId) return log("先测算", "err");
  try {
    const data = await api("/quotes/ai-suggest", { method: "POST", body: { calc_id: calcId, basis: "华东区中标样本（演示）" } });
    $("q-result").textContent = JSON.stringify(data, null, 2);
    log(`AI 参考价：${JSON.stringify(data.price_range)}`, "ok");
  } catch (e) { log(`AI 建议失败：${e}`, "err"); }
}

async function applyQuote() {
  if (!calcId) return log("先测算", "err");
  const list = await api(`/deliverables?project_id=${projectId}`);
  deliverables = list;
  const quote = list.find((d) => d.deliverable_type === 3);
  if (!quote) return log("没有报价单成果", "err");
  try {
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
      <a id="pkg-link" href="#"><button class="ghost">下载交付包</button></a>
    </div>
    <pre id="e-result" class="muted"></pre>`;
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
    $("pkg-link").href = `/api/v1/projects/${projectId}/delivery-package`;
    log(`导出完成：${data.files.length} 个文件`, "ok");
  } catch (e) { log(`导出失败：${e}`, "err"); }
}

/* ---------- 搜索/对话 ---------- */
function panelSearch() {
  $("panel").innerHTML = `
    <h3>搜索与对话</h3>
    <div class="row"><input id="s-query" placeholder="搜索关键词"><button onclick="doSearch()">搜索</button></div>
    <pre id="s-result" class="muted"></pre>
    <div class="row"><input id="c-msg" placeholder="向助手提问"><button onclick="doChat()">发送</button></div>`;
}

async function doSearch() {
  try {
    const data = await api("/searches", { method: "POST", body: { query: $("s-query").value } });
    $("s-result").textContent = JSON.stringify(data, null, 2);
    log(`搜索返回 ${data.results.length} 条`, "ok");
  } catch (e) { $("s-result").textContent = String(e); log(`搜索失败：${e}`, "err"); }
}

async function doChat() {
  if (!projectId) return log("先选用项目", "err");
  try {
    const t = await api(`/projects/${projectId}/tasks`, { method: "POST", body: { task_type: "chat", payload: { message: $("c-msg").value }, idempotency_key: `chat-${Date.now()}` } });
    pollTask(t.task_id);
  } catch (e) { log(`对话失败：${e}`, "err"); }
}

/* ---------- 初始化 ---------- */
renderTabs();
renderAuth();
renderPanel("auth");
