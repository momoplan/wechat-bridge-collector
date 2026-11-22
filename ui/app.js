import { dateInputsToEpochRange } from "./time-range.mjs";

const ids = [
  "runtime-badge", "refresh-button", "reload-status-button", "notice", "error",
  "chats-view", "contacts-view", "search-view", "status-view", "session-count",
  "session-filter", "session-list", "conversation-kind", "conversation-title",
  "message-count", "message-type", "history-start", "history-end",
  "apply-history-filter", "message-list", "load-older-button", "contact-count",
  "contact-form", "contact-query", "contact-list", "search-count", "search-form",
  "search-keyword", "search-scope", "current-conversation-option", "search-start",
  "search-end", "search-button", "search-results", "load-more-search", "status-grid",
  "setup-badge", "setup-title", "setup-detail", "collector-address", "pairing-code", "pair-button", "pairing-form",
];
const elements = Object.fromEntries(ids.map((id) => [id, document.getElementById(id)]));
const product = document.body.dataset.product || "本地消息";
const PAGE_SIZE = 50;

let activeView = "chats";
let sessions = [];
let selectedSession = null;
let historyMessages = [];
let historyOffset = 0;
let historyHasMore = false;
let searchMessages = [];
let searchOffset = 0;
let searchHasMore = false;
let lastSearchPayload = null;
let busy = new Set();

function bridge() {
  const api = window.baijimuLocalApp;
  if (!api || api.version !== 1 || typeof api.invoke !== "function") {
    throw new Error("当前百积木版本不支持本地应用界面，请升级后重试。");
  }
  return api;
}

function text(value, fallback = "") {
  const output = String(value ?? "").trim();
  return output || fallback;
}

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error || "操作失败");
}

function setNotice(message = "", error = false) {
  const target = error ? elements.error : elements.notice;
  const other = error ? elements.notice : elements.error;
  target.textContent = message;
  target.hidden = !message;
  if (message) {
    other.textContent = "";
    other.hidden = true;
  }
}

function setRuntimeBadge(label, tone = "neutral") {
  elements["runtime-badge"].textContent = label;
  elements["runtime-badge"].className = `status-badge ${tone}`;
}

function setBusy(key, value) {
  if (value) busy.add(key);
  else busy.delete(key);
  elements["refresh-button"].disabled = busy.size > 0;
  elements["reload-status-button"].disabled = busy.has("status");
  elements["apply-history-filter"].disabled = !selectedSession || busy.has("history");
  elements["load-older-button"].disabled = busy.has("history");
  elements["search-button"].disabled = busy.has("search");
  elements["load-more-search"].disabled = busy.has("search");
  elements["pair-button"].disabled = busy.has("pair");
}

function switchView(view) {
  activeView = view;
  for (const name of ["chats", "contacts", "search", "status"]) {
    elements[`${name}-view`].hidden = name !== view;
  }
  document.querySelectorAll("[data-view]").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  if (view === "contacts" && elements["contact-list"].dataset.loaded !== "true") {
    void loadContacts("");
  }
  if (view === "status" && elements["status-grid"].dataset.loaded !== "true") {
    void loadStatus();
  }
}

function sessionName(session) {
  return text(session?.conversationName, text(session?.displayName, text(session?.conversationId, "未知会话")));
}

function initials(value) {
  return Array.from(text(value, "?")).slice(0, 1).join("").toUpperCase();
}

function formatTime(value, includeDate = false) {
  const numeric = Number(value);
  const date = Number.isSafeInteger(numeric) && numeric > 0 ? new Date(numeric) : new Date(Number.NaN);
  if (!Number.isFinite(date.getTime())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", includeDate
    ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }
    : { hour: "2-digit", minute: "2-digit" }).format(date);
}

function renderSessions() {
  const filter = elements["session-filter"].value.trim().toLowerCase();
  const visible = sessions.filter((session) => {
    if (!filter) return true;
    return [sessionName(session), session?.conversationId, session?.summary]
      .some((value) => text(value).toLowerCase().includes(filter));
  });
  elements["session-count"].textContent = String(visible.length);
  elements["session-list"].replaceChildren();
  if (!visible.length) {
    elements["session-list"].append(empty(filter ? "没有符合条件的会话。" : "本地数据库中还没有可显示的会话。"));
    return;
  }
  visible.forEach((session) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `conversation-item${selectedSession?.conversationId === session.conversationId ? " active" : ""}`;
    const avatar = document.createElement("span");
    avatar.className = "avatar";
    avatar.textContent = session.isGroup ? "群" : initials(sessionName(session));
    const copy = document.createElement("span");
    copy.className = "conversation-copy";
    const title = document.createElement("strong");
    title.textContent = sessionName(session);
    const summary = document.createElement("span");
    summary.textContent = text(session.summary, text(session.conversationKind, "暂无摘要"));
    copy.append(title, summary);
    const meta = document.createElement("span");
    meta.className = "conversation-meta";
    const time = document.createElement("time");
    time.textContent = formatTime(session.lastTimestamp, true);
    meta.append(time);
    const unreadCount = Number(session.unreadCount) || 0;
    if (unreadCount > 0) {
      const unread = document.createElement("span");
      unread.className = "unread";
      unread.textContent = unreadCount > 99 ? "99+" : String(unreadCount);
      meta.append(unread);
    }
    button.append(avatar, copy, meta);
    button.addEventListener("click", () => void openSession(session));
    elements["session-list"].append(button);
  });
}

async function loadSessions(preserveSelection = true) {
  setBusy("sessions", true);
  setNotice();
  try {
    const result = await bridge().invoke("getRecentSessions", { limit: 100 });
    sessions = Array.isArray(result?.sessions) ? result.sessions : [];
    const previousId = preserveSelection ? selectedSession?.conversationId : "";
    selectedSession = sessions.find(
      (item) => item.conversationId === previousId,
    ) || null;
    renderSessions();
    setRuntimeBadge("运行中", "success");
    if (selectedSession) await loadHistory(false);
  } catch (error) {
    const message = errorMessage(error);
    const waitingForSource = message.includes("SOURCE_NOT_READY")
      || message.includes("数据库访问权限")
      || message.includes("数据库访问");
    setRuntimeBadge(waitingForSource ? "数据源未就绪" : "连接失败", waitingForSource ? "warning" : "danger");
    setNotice(message, true);
    elements["session-list"].replaceChildren(empty("无法读取会话，请查看数据源状态中的具体原因。"));
  } finally {
    setBusy("sessions", false);
  }
}

async function openSession(session) {
  selectedSession = session;
  historyMessages = [];
  historyOffset = 0;
  historyHasMore = false;
  elements["conversation-kind"].textContent = session.isGroup ? "群聊" : text(session.conversationKind, "单聊");
  elements["conversation-title"].textContent = sessionName(session);
  elements["current-conversation-option"].value = session.conversationId;
  elements["current-conversation-option"].textContent = `当前会话：${sessionName(session)}`;
  elements["current-conversation-option"].disabled = false;
  elements["apply-history-filter"].disabled = false;
  renderSessions();
  await loadHistory(false);
}

function selectedMessageTypes() {
  const value = elements["message-type"].value;
  if (!value) return undefined;
  return value === "file" ? ["file", "app"] : [value];
}

async function loadHistory(appendOlder) {
  if (!selectedSession || busy.has("history")) return;
  setBusy("history", true);
  setNotice();
  const offset = appendOlder ? historyOffset : 0;
  if (!appendOlder) {
    elements["message-list"].replaceChildren(empty("正在读取聊天记录…"));
  }
  try {
    const result = await bridge().invoke("getChatHistory", {
      conversationId: selectedSession.conversationId,
      limit: PAGE_SIZE,
      offset,
      ...dateInputsToEpochRange(
        elements["history-start"].value,
        elements["history-end"].value,
      ),
      oldestFirst: false,
      messageTypes: selectedMessageTypes(),
    });
    const page = Array.isArray(result?.messages) ? result.messages : [];
    historyMessages = appendOlder ? historyMessages.concat(page) : page;
    historyOffset = offset + page.length;
    historyHasMore = result?.hasMoreHint === true && page.length > 0;
    renderHistory();
  } catch (error) {
    setNotice(errorMessage(error), true);
    if (!appendOlder) elements["message-list"].replaceChildren(empty("聊天记录读取失败。"));
  } finally {
    setBusy("history", false);
  }
}

function renderHistory() {
  elements["message-list"].replaceChildren();
  elements["message-count"].textContent = `${historyMessages.length} 条`;
  elements["load-older-button"].hidden = !historyHasMore;
  if (!historyMessages.length) {
    elements["message-list"].append(empty("当前条件下没有找到消息。"));
    return;
  }
  [...historyMessages].reverse().forEach((message) => {
    elements["message-list"].append(messageCard(message));
  });
  if (historyOffset <= PAGE_SIZE) {
    elements["message-list"].scrollTop = elements["message-list"].scrollHeight;
  }
}

function messageCard(message) {
  const article = document.createElement("article");
  article.className = `message ${message?.direction === "outgoing" ? "outgoing" : "incoming"}`;
  const head = document.createElement("div");
  head.className = "message-head";
  const sender = document.createElement("strong");
  sender.textContent = message?.direction === "outgoing" ? "我" : text(message?.senderName, "未知发送者");
  const time = document.createElement("span");
  time.textContent = formatTime(message?.timestamp, true);
  head.append(sender, time);
  const content = document.createElement("p");
  content.className = "message-text";
  content.textContent = text(message?.text, `[${text(message?.messageTypeLabel, text(message?.messageType, "消息"))}]`);
  const foot = document.createElement("div");
  foot.className = "message-foot";
  const type = document.createElement("span");
  type.className = "type-chip";
  type.textContent = text(message?.messageTypeLabel, text(message?.messageType, "消息"));
  foot.append(type);
  article.append(head, content, foot);
  return article;
}

async function loadContacts(query) {
  setBusy("contacts", true);
  setNotice();
  elements["contact-list"].replaceChildren(empty("正在读取联系人…"));
  try {
    const result = await bridge().invoke("getContacts", { query, limit: 300 });
    const contacts = Array.isArray(result?.contacts) ? result.contacts : [];
    elements["contact-count"].textContent = String(contacts.length);
    elements["contact-list"].replaceChildren();
    elements["contact-list"].dataset.loaded = "true";
    if (!contacts.length) {
      elements["contact-list"].append(empty("没有找到联系人或群聊。"));
      return;
    }
    contacts.forEach((contact) => {
      const card = document.createElement("article");
      card.className = "contact-card";
      const avatar = document.createElement("span");
      avatar.className = "avatar";
      avatar.textContent = contact.isGroup ? "群" : initials(contact.displayName);
      const copy = document.createElement("div");
      copy.className = "contact-copy";
      const title = document.createElement("strong");
      title.textContent = text(contact.displayName, text(contact.username, "未知联系人"));
      const id = document.createElement("span");
      id.textContent = text(contact.remark, text(contact.username, text(contact.userId, "")));
      copy.append(title, id);
      card.append(avatar, copy);
      elements["contact-list"].append(card);
    });
  } catch (error) {
    setNotice(errorMessage(error), true);
    elements["contact-list"].replaceChildren(empty("联系人读取失败。"));
  } finally {
    setBusy("contacts", false);
  }
}

function currentSearchPayload(offset = 0) {
  return {
    keyword: elements["search-keyword"].value.trim(),
    conversationId: elements["search-scope"].value,
    ...dateInputsToEpochRange(
      elements["search-start"].value,
      elements["search-end"].value,
    ),
    limit: PAGE_SIZE,
    offset,
  };
}

async function runSearch(append) {
  if (busy.has("search")) return;
  let payload;
  try {
    payload = append && lastSearchPayload
      ? { ...lastSearchPayload, offset: searchOffset }
      : currentSearchPayload(0);
  } catch (error) {
    setNotice(errorMessage(error), true);
    return;
  }
  if (!payload.keyword) {
    setNotice("请输入要搜索的关键词。", true);
    return;
  }
  setBusy("search", true);
  setNotice();
  if (!append) elements["search-results"].replaceChildren(empty("正在搜索本地消息…"));
  try {
    const result = await bridge().invoke("searchMessages", payload);
    const page = Array.isArray(result?.messages) ? result.messages : [];
    searchMessages = append ? searchMessages.concat(page) : page;
    searchOffset = payload.offset + page.length;
    searchHasMore = result?.hasMoreHint === true && page.length > 0;
    lastSearchPayload = { ...payload, offset: 0 };
    renderSearchResults();
  } catch (error) {
    setNotice(errorMessage(error), true);
    if (!append) elements["search-results"].replaceChildren(empty("消息搜索失败。"));
  } finally {
    setBusy("search", false);
  }
}

function renderSearchResults() {
  elements["search-count"].textContent = `${searchMessages.length} 条`;
  elements["load-more-search"].hidden = !searchHasMore;
  elements["search-results"].replaceChildren();
  if (!searchMessages.length) {
    elements["search-results"].append(empty("没有找到匹配消息。"));
    return;
  }
  searchMessages.forEach((message) => {
    const item = document.createElement("article");
    item.className = "search-result";
    const conversation = document.createElement("strong");
    conversation.textContent = text(message.conversationName, text(message.conversationId, "未知会话"));
    const content = document.createElement("p");
    content.textContent = text(message.text, `[${text(message.messageTypeLabel, "非文本消息")}]`);
    const time = document.createElement("time");
    time.textContent = formatTime(message.timestamp, true);
    item.append(conversation, content, time);
    elements["search-results"].append(item);
  });
}

function renderSetup(sourceAccess) {
  const states = {
    ready: ["已连接", "success", "微信采集器已就绪"],
    checking: ["检测中", "warning", "采集器正在检查数据源"],
    failed: ["采集异常", "danger", "请检查独立采集器"],
    unavailable: ["未连接", "warning", "等待连接独立采集器"],
  };
  const [label, tone, title] = states[sourceAccess?.status] || states.unavailable;
  elements["setup-badge"].textContent = label;
  elements["setup-badge"].className = `status-badge ${tone}`;
  elements["setup-title"].textContent = title;
  elements["setup-detail"].textContent = text(sourceAccess?.detail);
  setRuntimeBadge(label, tone);
}

async function loadStatus(clearNotice = true) {
  setBusy("status", true);
  if (clearNotice) setNotice();
  elements["status-grid"].replaceChildren(statusItem("服务", "正在检测…"));
  try {
    const state = await bridge().invoke("runtimeState");
    renderSetup(state?.sourceAccess);
    const deliveryStates = {
      queued: "已由百积木受理",
      idle: "暂无待发事件",
      no_subscribers: "等待订阅，事件已保留",
      skipped_no_subscribers: "无订阅者，已结束投递",
      unavailable: "等待连接",
      rejected: "投递待重试",
      invalid_event: "采集器事件格式无效",
    };
    const values = [
      ["应用", text(state?.product, product)],
      ["版本", text(state?.version, "未知")],
      ["消息投递", text(deliveryStates[state?.delivery?.messageReceived], "等待检测")],
      ["联系人同步", text(deliveryStates[state?.delivery?.contactsChanged], "等待检测")],
    ];
    elements["status-grid"].replaceChildren(...values.map(([label, value]) => statusItem(label, value)));
    elements["status-grid"].dataset.loaded = "true";
  } catch (error) {
    renderSetup({status: "unavailable"});
    setNotice(errorMessage(error), true);
    elements["status-grid"].replaceChildren(statusItem("检测结果", "失败"));
  } finally {
    setBusy("status", false);
  }
}

async function pairCollector(event) {
  event.preventDefault();
  if (busy.has("pair")) return;
  setBusy("pair", true);
  setNotice();
  try {
    await bridge().invoke("pairCollector", {
      address: elements["collector-address"].value.trim(),
      code: elements["pairing-code"].value.trim(),
    });
    elements["pairing-code"].value = "";
    setNotice("配对已保存。");
    await loadStatus(false);
  } catch (error) {
    setNotice(errorMessage(error), true);
  } finally {
    setBusy("pair", false);
  }
}

function statusItem(label, value) {
  const wrapper = document.createElement("div");
  const term = document.createElement("dt");
  term.textContent = label;
  const description = document.createElement("dd");
  description.textContent = value;
  wrapper.append(term, description);
  return wrapper;
}

function empty(message) {
  const element = document.createElement("div");
  element.className = "empty-state";
  element.textContent = message;
  return element;
}

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.view));
});
elements["session-filter"].addEventListener("input", renderSessions);
elements["refresh-button"].addEventListener("click", () => {
  if (activeView === "status") void loadStatus();
  else if (activeView === "contacts") void loadContacts(elements["contact-query"].value.trim());
  else void loadSessions(true);
});
elements["reload-status-button"].addEventListener("click", () => void loadStatus());
elements["pairing-form"].addEventListener("submit", pairCollector);
elements["apply-history-filter"].addEventListener("click", () => void loadHistory(false));
elements["load-older-button"].addEventListener("click", () => void loadHistory(true));
elements["contact-form"].addEventListener("submit", (event) => {
  event.preventDefault();
  void loadContacts(elements["contact-query"].value.trim());
});
elements["search-form"].addEventListener("submit", (event) => {
  event.preventDefault();
  void runSearch(false);
});
elements["load-more-search"].addEventListener("click", () => void runSearch(true));

setRuntimeBadge("连接中", "neutral");
void (async () => {
  const state = await loadStatus(false);
  if (state?.sourceAccess?.status === "ready") {
    await loadSessions(false);
  } else {
    switchView("status");
  }
})();
