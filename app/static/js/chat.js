import { postRaw, apiDetail, readEventStream } from "./api.js";
import { $ } from "./dom.js";
import { renderMarkdown } from "./markdown.js";
import { scrollDown, stickToBottom } from "./scroll.js";
import { state } from "./state.js";
import { renderActions, renderHandoff } from "./actions.js";
import { renderFeedback } from "./feedback.js";
import { renderSources } from "./sources.js";
import { loadConversations } from "./conversations.js";

export const chatLog = $("chat-log");

// `into` lets history render older messages off-screen before prepending them.
export function addTurn(kind, into = chatLog) {
  $("chat-empty").hidden = true;
  // Only the newest turn offers Get Help (see renderHandoff).
  if (into === chatLog) chatLog.querySelectorAll(".handoff").forEach((el) => el.remove());
  const turn = document.createElement("div");
  turn.className = `turn ${kind}`;
  into.appendChild(turn);
  return turn;
}

export function addUserBubble(text, into = chatLog, files = []) {
  const turn = addTurn("user", into);
  const b = document.createElement("div");
  b.className = "bubble";
  b.textContent = text;
  for (const name of files) {
    const chip = document.createElement("div");
    chip.className = "attachment";
    chip.textContent = `Attachment: ${name}`;
    b.appendChild(chip);
  }
  turn.appendChild(b);
  if (into !== chatLog) return;
  stickToBottom();
  scrollDown();
}

export function addErrorLine(text) {
  const el = document.createElement("div");
  el.className = "error";
  el.textContent = text;
  chatLog.appendChild(el);
  scrollDown();
}

function addBubble(turn, text) {
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  renderMarkdown(bubble, text);
  turn.appendChild(bubble);
  return bubble;
}

// One assistant or live agent message as the server presents it (live, from
// history, or from an event).
export function renderAssistantMessage(turn, message, conversationId) {
  if (message.message_id) state.shownMessageIds.add(message.message_id);
  // A human agent's replies arrive through the same resources as the
  // assistant's; `actor` and `sender_info` are the only way to tell them apart.
  if (message.actor === "LIVE_AGENT") {
    const header = document.createElement("div");
    header.className = "agent-header";
    if (message.sender && message.sender.avatar_url) header.appendChild(agentAvatar(message.sender.avatar_url));
    header.append((message.sender && message.sender.display_name) || "Live agent");
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = "Live agent";
    header.appendChild(tag);
    turn.appendChild(header);
  }
  addBubble(turn, message.text);
  renderSources(turn, message.sources);
  const ref = { conversation_id: conversationId, response_id: message.response_id, message_id: message.message_id };
  if (message.actions.length) renderActions(turn, { ...ref, actions: message.actions });
  if (message.handoff.length) renderHandoff(turn, { ...ref, categories: message.handoff });
  if (message.feedback.helpful || message.feedback.unhelpful) renderFeedback(turn, { ...ref, ...message.feedback });
}

// The avatar is hosted by the live chat provider, outside this app. Send no
// referrer, and drop the image if it fails to load.
function agentAvatar(url) {
  const img = document.createElement("img");
  img.className = "agent-avatar";
  img.alt = "";
  img.referrerPolicy = "no-referrer";
  img.addEventListener("error", () => img.remove());
  img.src = url;
  return img;
}

export async function sendMessage({ text, callbackId, echo }) {
  if (echo) addUserBubble(echo);
  $("chat-input").disabled = true;
  $("send-btn").disabled = true;
  const ctx = { turn: addTurn("assistant"), reasoning: null, bubble: null };
  try {
    const resp = await postRaw("/api/message", {
      text: text || null,
      callback_id: callbackId || null,
      conversation_id: state.conversationId,
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(apiDetail(body, `request failed (${resp.status})`));
    }
    await readEventStream(resp, (frame) => handleFrame(frame, ctx));
  } catch (e) {
    addErrorLine(String(e.message || e));
  } finally {
    $("chat-input").disabled = false;
    $("send-btn").disabled = false;
    $("chat-input").focus();
  }
}

// REASONING_MESSAGE snapshots are cumulative: each one replaces the list.
function renderReasoning(ctx, text) {
  if (!ctx.reasoning) {
    ctx.reasoning = document.createElement("details");
    ctx.reasoning.className = "reasoning";
    ctx.reasoning.open = true;
    ctx.reasoning.innerHTML = "<summary>Reasoning</summary><ol></ol>";
    ctx.turn.prepend(ctx.reasoning);
  }
  const steps = text.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
    const li = document.createElement("li");
    renderMarkdown(li, line);
    return li;
  });
  ctx.reasoning.querySelector("ol").replaceChildren(...steps);
}

function handleFrame({ event, data }, ctx) {
  if (event === "conversation") {
    state.conversationId = data.conversation_id;
    loadConversations();
  } else if (event === "delta" && data.output_type === "REASONING_MESSAGE") {
    renderReasoning(ctx, data.text || "");
  } else if (event === "delta" && data.output_type === "MESSAGE") {
    if (ctx.reasoning) ctx.reasoning.open = false;
    if (!ctx.bubble) ctx.bubble = addBubble(ctx.turn, "");
    renderMarkdown(ctx.bubble, data.text || "");
    renderSources(ctx.turn, data.sources);
  } else if (event === "completed") {
    if (data.status === "FAILED") addErrorLine("The assistant could not complete that response. Try again, or rephrase.");
  } else if (event === "late_message") {
    if (state.shownMessageIds.has(data.message.message_id)) return;
    // A reply the API filed under an earlier response (the Exit Live Chat
    // farewell): fill this turn if it is still empty, else add a turn.
    const turn = ctx.bubble ? addTurn("assistant") : ctx.turn;
    renderAssistantMessage(turn, data.message, data.conversation_id);
    ctx.bubble = ctx.bubble || turn.querySelector(".bubble");
  } else if (event === "actions" || event === "handoff" || event === "feedback_ids") {
    if (data.message_id) state.shownMessageIds.add(data.message_id);
    ({ actions: renderActions, handoff: renderHandoff, feedback_ids: renderFeedback })[event](ctx.turn, data);
  } else if (event === "error") {
    addErrorLine(data.message || "Something went wrong.");
  }
  scrollDown();
}
