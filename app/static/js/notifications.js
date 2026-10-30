import { $ } from "./dom.js";
import { renderMarkdown } from "./markdown.js";
import { scrollDown } from "./scroll.js";
import { state } from "./state.js";
import { addStatusLine, addTurn, renderAssistantMessage } from "./chat.js";
import { openConversation } from "./conversations.js";

let source = null;
let unread = 0;
// One consent can produce several AUTHORIZE events; the first marks completion.
const authorizedConversations = new Set();

export function connectNotifications() {
  closeNotifications();
  source = new EventSource("/api/events");
  source.addEventListener("notification", (ev) => {
    const data = JSON.parse(ev.data);
    // Async follow-ups (ticket confirmations, live agent updates) arrive as
    // events on the SAME conversation after the response already completed.
    // Those belong in the thread; only other conversations' events are
    // notifications.
    const status = statusText(data);
    if (data.conversation_id && data.conversation_id === state.conversationId) {
      if (!data.message) {
        if (status) addStatusLine(status);
        return;
      }
      if (data.message.message_id && state.shownMessageIds.has(data.message.message_id)) return;
      const turn = addTurn("assistant");
      renderAssistantMessage(turn, data.message, data.conversation_id);
      // Follow-ups land in the thread without a reply in flight, so announce them here.
      const spoken = turn.querySelector(".bubble")?.textContent || "";
      scrollDown();
      if (status) addStatusLine(status, { announce: false });
      $("reply-status").textContent = [spoken, status].filter(Boolean).join(" ");
      return;
    }
    $("notif-empty").hidden = true;
    const item = document.createElement("div");
    item.className = "notif-item";
    const body = document.createElement("div");
    renderMarkdown(body, data.text || status || "(no text)");
    const meta = document.createElement("div");
    meta.className = "muted";
    const author = data.actor === "LIVE_AGENT" ? (data.message?.sender?.display_name || "Live agent") : (data.actor || "");
    meta.textContent = [data.created_at, author, data.message ? status : ""].filter(Boolean).join(" · ");
    item.append(body, meta);
    if (data.conversation_id) {
      // The body can hold links, so a button inside the item opens the
      // conversation rather than making the whole item a button.
      const open = document.createElement("button");
      open.type = "button";
      open.textContent = "Open conversation";
      open.addEventListener("click", () => { $("notif-drawer").hidden = true; openConversation(data.conversation_id); });
      meta.append(" · ", open);
    }
    $("notif-list").prepend(item);
    if ($("notif-drawer").hidden) {
      unread += 1;
      $("notif-badge").textContent = String(unread);
      $("notif-badge").hidden = false;
    }
  });
}

// Read from the event's category details, never from the message text.
function statusText(data) {
  if (data.live_agent_session_state === "ENDED") return "Live agent chat ended";
  if (data.category !== "USER_CONSENT_AUTH") return "";
  if (data.consent_operation_type === "AUTHORIZE") {
    if (authorizedConversations.has(data.conversation_id)) return "";
    authorizedConversations.add(data.conversation_id);
    return "Account connected";
  }
  if (data.consent_operation_type === "REVOKE") {
    authorizedConversations.delete(data.conversation_id);
    return "Account disconnected";
  }
  return "";
}

export function closeNotifications() {
  if (source) source.close();
  source = null;
  unread = 0;
  authorizedConversations.clear();
  $("notif-badge").hidden = true;
}

export function toggleNotifications() {
  const drawer = $("notif-drawer");
  drawer.hidden = !drawer.hidden;
  if (!drawer.hidden) {
    unread = 0;
    $("notif-badge").hidden = true;
  }
}
