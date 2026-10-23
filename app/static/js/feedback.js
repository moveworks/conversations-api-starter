import { postJson } from "./api.js";
import { toast } from "./toast.js";

export function renderFeedback(turn, data) {
  const row = document.createElement("div");
  row.className = "feedback-row";
  for (const [kind, icon, label] of [["helpful", "👍", "Helpful"], ["unhelpful", "👎", "Not helpful"]]) {
    if (!data[kind]) continue;
    const btn = document.createElement("button");
    btn.className = "feedback-btn";
    btn.textContent = icon;
    btn.title = label;
    btn.addEventListener("click", () => {
      // A thumbs-down is where a reason helps most; the API takes it as
      // `additional_feedback` on the same call.
      if (kind === "unhelpful") openComment(row, btn, data);
      else submit(row, btn, data, kind, "");
    });
    row.appendChild(btn);
  }
  if (row.children.length) turn.appendChild(row);
}

// Send attaches the comment; Skip sends the thumbs-down without one.
function openComment(row, btn, data) {
  if (row.nextElementSibling?.classList.contains("feedback-comment")) return;
  const box = document.createElement("form");
  box.className = "feedback-comment";
  const input = document.createElement("textarea");
  input.rows = 2;
  input.maxLength = 2000;
  input.placeholder = "What was wrong? (not required)";
  const send = document.createElement("button");
  send.type = "submit";
  send.textContent = "Send";
  const skip = document.createElement("button");
  skip.type = "button";
  skip.textContent = "Skip";
  const finish = async (comment) => {
    send.disabled = skip.disabled = true;
    if (await submit(row, btn, data, "unhelpful", comment)) box.remove();
    else send.disabled = skip.disabled = false;
  };
  box.addEventListener("submit", (e) => { e.preventDefault(); finish(input.value); });
  skip.addEventListener("click", () => finish(""));
  box.append(input, send, skip);
  row.after(box);
  input.focus();
}

async function submit(row, btn, data, kind, comment) {
  row.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    await postJson("/api/feedback", {
      conversation_id: data.conversation_id,
      response_id: data.response_id,
      message_id: data.message_id,
      callback_id: data[kind],
      additional_feedback: comment.trim() || null,
    }, "feedback failed");
    btn.classList.add("sent");
    // A vote from either button settles the message, so close an open comment box.
    if (row.nextElementSibling?.classList.contains("feedback-comment")) row.nextElementSibling.remove();
    toast(kind === "helpful" ? "Thanks, feedback recorded." : "Feedback recorded.");
    return true;
  } catch (e) {
    row.querySelectorAll("button").forEach((b) => (b.disabled = false));
    toast(String(e.message || e));
    return false;
  }
}
