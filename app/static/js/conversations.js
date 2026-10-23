import { getJson, patchJson } from "./api.js";
import { $ } from "./dom.js";
import { scrollDown, stickToBottom } from "./scroll.js";
import { state } from "./state.js";
import { toast } from "./toast.js";
import { addTurn, addUserBubble, chatLog, renderAssistantMessage, resetChat } from "./chat.js";
import { dropModalReads } from "./modals.js";

const PAGE = 20;
let showArchived = false;

export async function loadConversations() {
  try {
    // ListConversations returns `title`, so each page of the sidebar costs one request.
    const body = await getJson(`/api/conversations?limit=${PAGE}&archived=${showArchived}`);
    const list = $("conv-list");
    list.replaceChildren();
    const conversations = body.conversations || [];
    if (!conversations.length) {
      const li = document.createElement("li");
      li.className = "conv-empty";
      li.textContent = showArchived ? "No archived conversations." : "No conversations yet. Send a message to start one.";
      list.appendChild(li);
      return;
    }
    appendConversations(list, conversations, body.metadata?.next_cursor);
  } catch { /* the sidebar is best-effort */ }
}

function appendConversations(list, conversations, nextCursor) {
  for (const c of conversations) list.appendChild(conversationItem(c));
  if (!nextCursor) return;
  const li = document.createElement("li");
  li.className = "conv-more";
  const btn = document.createElement("button");
  btn.textContent = "Load more";
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      const body = await getJson(`/api/conversations?limit=${PAGE}&archived=${showArchived}&cursor=${encodeURIComponent(nextCursor)}`);
      li.remove();
      appendConversations(list, body.conversations || [], body.metadata?.next_cursor);
    } catch (e) {
      btn.disabled = false;
      toast(String(e.message || e));
    }
  });
  li.appendChild(btn);
  list.appendChild(li);
}

function conversationItem(c) {
  const li = document.createElement("li");
  li.className = "conv-item" + (c.conversation_id === state.conversationId ? " active" : "");
  li.dataset.id = c.conversation_id;
  // The title is a button so the conversation opens from the keyboard too.
  const preview = document.createElement("button");
  preview.type = "button";
  preview.className = "conv-title";
  preview.title = c.conversation_id;
  preview.addEventListener("click", () => openConversation(c.conversation_id));
  preview.textContent = (c.title || "").trim() || "New conversation";
  const button = (label, fn) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", (e) => { e.stopPropagation(); fn(); });
    return b;
  };
  li.append(
    preview,
    button("Rename", () => rename(c, preview)),
    button(showArchived ? "Unarchive" : "Archive", () => setArchived(li, c, !showArchived)),
  );
  return li;
}

// Edit the title in place: Enter saves, Escape (or leaving the field) cancels.
function rename(c, preview) {
  if (preview.hidden) return;
  const input = document.createElement("input");
  input.className = "conv-title";
  input.value = preview.textContent;
  input.maxLength = 200;
  const close = () => { input.remove(); preview.hidden = false; };
  input.addEventListener("click", (e) => e.stopPropagation());
  input.addEventListener("blur", close);
  input.addEventListener("keydown", async (e) => {
    if (e.key === "Escape") close();
    if (e.key !== "Enter") return;
    e.preventDefault();
    const title = input.value.trim();
    if (title && title !== preview.textContent) {
      try {
        await patchJson(`/api/conversations/${c.conversation_id}`, { title }, "rename failed");
        preview.textContent = c.title = title;
      } catch (err) {
        toast(String(err.message || err));
      }
    }
    close();
  });
  preview.hidden = true;
  preview.after(input);
  input.focus();
  input.select();
}

async function setArchived(li, c, archived) {
  try {
    await patchJson(`/api/conversations/${c.conversation_id}`, { archived }, "update failed");
    li.remove();
    toast(archived ? "Archived." : "Moved back to conversations.");
    if (!$("conv-list").querySelector(".conv-item")) loadConversations();
  } catch (e) {
    toast(String(e.message || e));
  }
}

export function toggleArchivedView() {
  showArchived = !showArchived;
  const btn = $("conv-archived-toggle");
  btn.setAttribute("aria-pressed", String(showArchived));
  btn.textContent = showArchived ? "Back" : "Archived";
  $("conv-heading").textContent = showArchived ? "Archived" : "Conversations";
  loadConversations();
}

function markActiveConversation() {
  document.querySelectorAll(".conv-item").forEach((li) => {
    li.classList.toggle("active", li.dataset.id === state.conversationId);
  });
}

// History records a button press without its label, so it reads as a note, not a bubble.
function addSysline(text, into) {
  const el = document.createElement("div");
  el.className = "sysline";
  el.textContent = text;
  into.appendChild(el);
}

function renderMessages(messages, id, into) {
  for (const m of messages) {
    if (m.actor === "USER" && m.is_action) addSysline("Selected an option", into);
    else if (m.actor === "USER") addUserBubble(m.text, into, m.files || []);
    else renderAssistantMessage(addTurn("assistant", into), m, id);
  }
}

// Each open gets a number, so a slower earlier load of the same conversation
// cannot render on top of a newer one.
let openSeq = 0;

// Disconnecting abandons any history load still in flight.
export function cancelOpen() {
  openSeq += 1;
}

export async function openConversation(id) {
  const seq = ++openSeq;
  resetChat();
  dropModalReads();
  state.conversationId = id;
  markActiveConversation();
  if (window.innerWidth <= 1000) $("conv-sidebar").hidden = true;
  chatLog.replaceChildren();
  $("chat-empty").hidden = true;
  const loading = document.createElement("div");
  loading.className = "sysline";
  loading.textContent = "Loading conversation…";
  chatLog.appendChild(loading);
  try {
    const { messages, next_cursor: nextCursor } = await getJson(`/api/conversations/${id}/messages`);
    if (seq !== openSeq) return; // opened again meanwhile
    loading.remove();
    renderMessages(messages, id, chatLog);
    if (nextCursor) chatLog.prepend(olderButton(id, nextCursor));
    stickToBottom();
    scrollDown();
  } catch (e) {
    if (seq !== openSeq) return;
    loading.replaceChildren();
    loading.className = "error";
    loading.append(`Could not load this conversation. ${e.message || e} `);
    const retry = document.createElement("button");
    retry.className = "chip";
    retry.textContent = "Try again";
    retry.addEventListener("click", () => openConversation(id));
    loading.appendChild(retry);
  }
}

// History pages newest first; each older page goes above what is shown,
// keeping the reader's place.
function olderButton(id, cursor) {
  const wrap = document.createElement("div");
  wrap.className = "load-older";
  const btn = document.createElement("button");
  btn.textContent = "Load older messages";
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      const { messages, next_cursor: nextCursor } = await getJson(`/api/conversations/${id}/messages?cursor=${encodeURIComponent(cursor)}`);
      if (state.conversationId !== id) return;
      const older = document.createDocumentFragment();
      const holder = document.createElement("div");
      renderMessages(messages, id, holder);
      holder.querySelectorAll(".handoff").forEach((el) => el.remove());
      older.append(...holder.childNodes);
      if (nextCursor) older.prepend(olderButton(id, nextCursor));
      const scroller = $("chat-scroll");
      const fromBottom = scroller.scrollHeight - scroller.scrollTop;
      wrap.replaceWith(older);
      scroller.scrollTop = scroller.scrollHeight - fromBottom;
    } catch (e) {
      btn.disabled = false;
      toast(String(e.message || e));
    }
  });
  wrap.appendChild(btn);
  return wrap;
}

export function startNewConversation() {
  openSeq += 1; // a history load still in flight must not fill the new chat
  resetChat();
  dropModalReads();
  state.conversationId = null;
  markActiveConversation();
  chatLog.replaceChildren();
  $("chat-empty").hidden = false;
  $("chat-input").focus();
}
