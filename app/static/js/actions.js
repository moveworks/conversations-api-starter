import { renderMarkdown } from "./markdown.js";
import { toast } from "./toast.js";
import { sendMessage } from "./chat.js";
import { fetchModalCached, openModal } from "./modals.js";

export function actionChip(action, ref) {
  if (action.type === "URL_ACTION" && action.url) {
    const a = document.createElement("a");
    a.className = "chip";
    a.href = action.url;
    a.target = "_blank";
    a.rel = "noopener";
    // A portal-link form carries only "Complete this request"; the server
    // names it from the citation its short link resolves to.
    a.textContent = action.is_auth_link ? `Connect ${action.label}` : (action.form?.title || action.label);
    if (action.form?.summary) a.title = action.form.summary;
    if (action.is_auth_link) {
      // The assistant resumes the interrupted plugin on the user's next turn.
      a.addEventListener("click", () => {
        toast("Grant access in the new tab, then send your next message. The assistant picks up where it left off.");
      });
    }
    return a;
  }
  const btn = document.createElement("button");
  btn.className = "chip";
  btn.textContent = action.label;
  if (action.type === "CALLBACK_ACTION" && action.callback_id) {
    btn.addEventListener("click", () => {
      lockActionRow(btn);
      sendMessage({ callbackId: action.callback_id, echo: action.label });
    });
  } else if (action.type === "MODAL_ACTION" && action.modal_id) {
    btn.addEventListener("click", () => openModal({ ...ref, modal_id: action.modal_id }, btn.textContent, btn));
  } else {
    btn.disabled = true;
    btn.title = "This action type is not supported by this app.";
  }
  return btn;
}

export async function renderActions(turn, data) {
  const ref = { conversation_id: data.conversation_id, response_id: data.response_id, message_id: data.message_id };
  const row = document.createElement("div");
  row.className = "actions-row";
  const chips = data.actions.map((action) => row.appendChild(actionChip(action, ref)));
  turn.appendChild(row);

  // Several forms can arrive as identical "Complete this request" buttons;
  // the form's name is inside the modal. One fetch at a time, to stay inside
  // the rate limit.
  for (const [i, action] of data.actions.entries()) {
    if (action.type !== "MODAL_ACTION" || !action.modal_id) continue;
    try {
      const name = formName(await fetchModalCached({ ...ref, modal_id: action.modal_id }));
      if (name) chips[i].textContent = name;
    } catch { /* keep the API's label */ }
  }
  distinguishSharedNames(chips, data.actions);
}

// Distinct forms can share a name, such as two catalog items both called
// "Password Reset". The first line of each form's description tells them apart.
function distinguishSharedNames(chips, actions) {
  const counts = new Map();
  for (const chip of chips) counts.set(chip.textContent, (counts.get(chip.textContent) || 0) + 1);
  chips.forEach((chip, i) => {
    const summary = actions[i].form?.summary;
    if (summary && counts.get(chip.textContent) > 1) chip.textContent += ` (${summary})`;
  });
}

// The modal's first HEADER display field names the form.
function formName(modal) {
  for (const f of modal.fields || []) {
    const t = f.type === "DISPLAY" && (f.display || {}).text;
    if (t && t.style === "HEADER" && t.text) return t.text.replace(/\*\*/g, "").replace(/\s+/g, " ").trim();
  }
  return "";
}

// Once one action in a message is taken, the rest are stale: a user should
// not be able to "Edit request" something they already confirmed.
export function lockActionRow(chip) {
  const turn = chip.closest(".turn");
  if (!turn) return;
  for (const el of turn.querySelectorAll(":scope > .actions-row > .chip")) {
    if (el === chip) continue;
    if (el.tagName === "BUTTON") el.disabled = true;
    el.classList.add("stale");
    el.title = "Another option was chosen for this message";
  }
}

// Message.handoff, the Get Help menu, arrives on ordinary assistant turns,
// including mid-handoff status messages. Native surfaces show Get Help as one
// persistent control, so the menu sits behind a single button and only the
// newest turn offers it (addTurn retires older ones).
export function renderHandoff(turn, data) {
  const ref = { conversation_id: data.conversation_id, response_id: data.response_id, message_id: data.message_id };
  const wrap = document.createElement("details");
  wrap.className = "handoff";
  const summary = document.createElement("summary");
  summary.className = "chip";
  summary.textContent = "Get help";
  wrap.appendChild(summary);
  for (const category of data.categories) {
    const h = document.createElement("h4");
    h.textContent = category.name || "Get help";
    wrap.appendChild(h);
    if (category.description) {
      const d = document.createElement("p");
      d.className = "muted";
      d.textContent = category.description;
      wrap.appendChild(d);
    }
    const items = document.createElement("div");
    items.className = "actions-row";
    for (const item of category.items) {
      if (item.action) {
        items.appendChild(actionChip({ ...item.action, label: item.title || item.action.label }, ref));
      } else if (item.text) {
        const t = document.createElement("div");
        renderMarkdown(t, item.text);
        items.appendChild(t);
      }
    }
    wrap.appendChild(items);
  }
  turn.appendChild(wrap);
}
