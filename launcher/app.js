"use strict";

let sessionToken = "";
let bots = [];
let latest = null;
let lastFolder = null;
let inspectionText = null;
const $ = (id) => document.getElementById(id);

function option(select, value, label) {
  const node = document.createElement("option");
  node.value = value;
  node.textContent = label;
  select.appendChild(node);
}
function distinctSources() {
  const out = [];
  const seen = new Set();
  for (const bot of bots) {
    const key = bot.model + " | " + bot.source;
    if (!seen.has(key)) {
      out.push({ key, label: bot.model + "  ·  " + bot.source });
      seen.add(key);
    }
  }
  return out;
}
function fillSources(prefix, preferred) {
  const element = $(prefix + "Source");
  const old = element.value;
  element.replaceChildren();
  for (const source of distinctSources()) option(element, source.key, source.label);
  const sources = Array.from(element.options).map(o => o.value);
  if (sources.includes(old)) element.value = old;
  else if (preferred) {
    const found = sources.find(s => s.toLowerCase().includes(preferred.toLowerCase()));
    if (found) element.value = found;
  }
  fillScripts(prefix);
}
function fillScripts(prefix) {
  const select = $(prefix + "Script");
  const old = select.value;
  const source = $(prefix + "Source").value;
  select.replaceChildren();
  const filtered = bots.filter(b => b.model + " | " + b.source === source);
  for (const bot of filtered) option(select, bot.id, bot.agent + " / " + bot.script);
  if (Array.from(select.options).some(x => x.value === old)) select.value = old;
  fillInfo(prefix);
}
function fillInfo(prefix) {
  const selected = bots.find(b => b.id === $(prefix + "Script").value);
  $(prefix + "Info").textContent = selected
    ? "脚本：" + selected.path + "\n来源：" + selected.source
    : "当前来源尚未发现 bot.py / CompetitiveBot。";
}
function opponentUI() {
  const mode = $("opponent").value;
  $("rightBot").hidden = mode !== "bot";
  $("rightComputer").hidden = mode === "bot";
  $("difficultyLine").hidden = mode !== "computer";
  if (mode === "human") {
    $("realtime").checked = true;
    $("realtime").disabled = true;
  } else $("realtime").disabled = false;
}
function message(text, error = false) {
  const banner = $("banner");
  banner.hidden = !text;
  banner.textContent = text || "";
  banner.style.borderColor = error ? "#a65c5c" : "#397e95";
  banner.style.background = error ? "#3b2029" : "#163443";
  banner.style.color = error ? "#ffb1b5" : "#b6e5f8";
}
async function getJSON(url) {
  const res = await fetch(url, { cache: "no-store" });
  const data = await res.json();
  if (!res.ok) throw Error(data.error || ("HTTP " + res.status));
  return data;
}
async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Arena-Token": sessionToken },
    body: JSON.stringify(body)
  });
  const data = await res.json();
  if (!res.ok) throw Error(data.error || ("HTTP " + res.status));
  return data;
}
function parameters() {
  return {
    left: $("leftScript").value,
    opponent: $("opponent").value,
    right: $("rightScript").value,
    race: $("race").value,
    difficulty: $("difficulty").value,
    map: $("map").value,
    duration: Number($("duration").value),
    seed: Number($("seed").value),
    realtime: $("realtime").checked,
    model_label: $("modelLabel").value
  };
}
function textNode(parent, tag, value, cls) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  e.textContent = value;
  parent.appendChild(e);
  return e;
}
function showHistory(items) {
  const element = $("history");
  element.replaceChildren();
  for (const item of items.slice(0, 7)) {
    const div = document.createElement("div");
    div.className = "history-item";
    textNode(div, "strong", item.status);
    textNode(div, "small", item.id);
    if (item.report && item.report.results)
      textNode(div, "small", "结果：" + item.report.results.join(" / "));
    element.appendChild(div);
  }
}
function showStatus(process) {
  const status = process.status || "IDLE";
  const playing = !!process.running;
  $("matchStatus").textContent = playing ? "IN MATCH" : status.toUpperCase();
  $("matchHint").textContent = playing ? "比赛正在后台进行，自动更新日志" : "不运行游戏";
  $("startBtn").disabled = playing || bots.length === 0;
  $("stopBtn").disabled = !playing;
  $("inspectBtn").disabled = playing;
  $("health").textContent = "本地服务在线";
  $("healthDot").classList.remove("off");
  if (process.folder) {
    const report = process.report;
    const lines = ["当前记录目录：" + process.folder, "状态：" + status];
    if (process.exit_code !== undefined && process.exit_code !== null) lines.push("退出码：" + process.exit_code);
    if (report && report.results) lines.push("结果：" + report.results.join(" / "));
    if (report && report.replay_path) lines.push("回放：" + (report.replay_saved ? report.replay_path : "未生成有效回放"));
    if (report && report.errors && report.errors.length) lines.push("报错：" + report.errors.join(" | "));
    $("runSummary").textContent = lines.join("\n");
  }
}
function updateGame(info) {
  $("gameBuild").textContent = info.available ? info.build : "未找到客户端";
  $("gameRoot").textContent = info.root || "未定位";
  $("mapCount").textContent = info.maps.length;
  const select = $("map");
  const old = select.value;
  select.replaceChildren();
  for (const name of info.maps) option(select, name, name);
  if (info.maps.includes(old)) select.value = old;
  else if (info.maps.includes("RedCityLE")) select.value = "RedCityLE";
  if (!info.available) message("未找到 SC2_x64.exe。请先通过 Battle.net 更新完整游戏安装，随后刷新页面。", true);
  else if (!info.maps.length) message("SC2 已找到，但 Maps 文件夹里没有 .SC2Map。", true);
}
async function reload(first = false) {
  try {
    const data = await getJSON("/api/state");
    sessionToken = data.token;
    const signature = JSON.stringify(data.bots);
    if (first || signature !== latest) {
      bots = data.bots;
      latest = signature;
      fillSources("left", "webgpt");
      fillSources("right", "mizuki");
      $("botCount").textContent = bots.length;
      $("pythonPath").textContent = data.python;
      if (first) updateGame(data.game);
    }
    showStatus(data.process);
    showHistory(data.history);
    if (data.process.folder !== lastFolder) {
      lastFolder = data.process.folder;
      if (lastFolder) $("consoleLog").textContent = "获取日志中…";
    }
  } catch (error) {
    $("health").textContent = "连接断开";
    $("healthDot").classList.add("off");
    message("连接本地服务失败：" + error.message, true);
  }
}
async function refreshLog() {
  try {
    if (inspectionText && !lastFolder) {
      $("consoleLog").textContent = inspectionText;
      return;
    }
    const info = await getJSON("/api/log");
    const element = $("consoleLog");
    if (element.textContent !== info.log) {
      const nearBottom = element.scrollTop + element.clientHeight >= element.scrollHeight - 40;
      element.textContent = info.log;
      if (nearBottom) element.scrollTop = element.scrollHeight;
    }
    $("logTime").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  } catch (error) {
    $("consoleLog").textContent = "读取日志失败：" + error.message;
  }
}
async function action(name) {
  let target = null;
  try {
    if (name === "start" && !confirm("确认启动真实 StarCraft II 比赛？可能同时打开两份游戏客户端。")) return;
    if (name === "start") inspectionText = null;
    if (name === "stop" && !confirm("确认强制停止本次比赛及其 SC2 进程？可能不会保存 Replay。")) return;
    const btn = $(name + "Btn");
    target = btn;
    target.disabled = true;
    message(name === "inspect" ? "静态检查中，不会启动游戏。" : name === "start" ? "正在创建比赛…" : "停止中…");
    const result = await postJSON("/api/" + name, name === "stop" ? {} : parameters());
    if (name === "inspect") {
      inspectionText = result.output || "没有输出";
      $("consoleLog").textContent = inspectionText;
      message(result.pass ? "静态检查通过；没有启动游戏。" : "静态检查未通过，请查看下方日志。", !result.pass);
    } else {
      message(name === "start" ? "比赛进程已经创建，等待 SC2 API 启动并连接。" : "已发送停止命令。");
    }
  } catch (error) {
    message(error.message, true);
    $("consoleLog").textContent += "\n[GUI] " + error.message + "\n";
  } finally {
    await reload();
    if (name !== "inspect") await refreshLog();
    if (target && name === "inspect") target.disabled = false;
  }
}
document.addEventListener("DOMContentLoaded", async () => {
  $("leftSource").addEventListener("change", () => fillScripts("left"));
  $("rightSource").addEventListener("change", () => fillScripts("right"));
  $("leftScript").addEventListener("change", () => fillInfo("left"));
  $("rightScript").addEventListener("change", () => fillInfo("right"));
  $("opponent").addEventListener("change", opponentUI);
  $("startBtn").addEventListener("click", () => action("start"));
  $("inspectBtn").addEventListener("click", () => action("inspect"));
  $("stopBtn").addEventListener("click", () => action("stop"));
  $("refreshBtn").addEventListener("click", async () => { await reload(); await refreshLog(); });
  $("copyLogBtn").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("consoleLog").textContent); message("日志已复制。"); }
    catch { message("浏览器未允许访问剪贴板，请手动选中日志复制。", true); }
  });
  opponentUI();
  await reload(true);
  setInterval(async () => { await reload(); await refreshLog(); }, 2200);
});
