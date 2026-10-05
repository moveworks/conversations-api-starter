/* Moveworks Conversations API reference app. Plain ES modules, no build step. */
import { $ } from "./dom.js";
import { boot, connect, disconnect } from "./connect.js";
import { sendMessage } from "./chat.js";
import { startNewConversation, toggleArchivedView } from "./conversations.js";
import { closeModal, submitModal } from "./modals.js";
import { toggleNotifications } from "./notifications.js";

const input = $("chat-input");

// Grow with the text; scroll only once the box reaches its cap. scrollHeight
// excludes the border, so add it back or the box always overflows by 2px.
function autosizeComposer() {
  input.style.height = "auto";
  const border = input.offsetHeight - input.clientHeight;
  const cap = window.innerHeight * 0.6;
  const needed = input.scrollHeight + border;
  input.style.height = Math.min(needed, cap) + "px";
  input.style.overflowY = needed > cap ? "auto" : "hidden";
}

input.addEventListener("input", autosizeComposer);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("composer").requestSubmit();
  }
});
$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  autosizeComposer();
  sendMessage({ text, echo: text });
});

$("connect-form").addEventListener("submit", (e) => { e.preventDefault(); connect(); });
$("disconnect-btn").addEventListener("click", disconnect);
$("new-conv-btn").addEventListener("click", startNewConversation);
$("conv-archived-toggle").addEventListener("click", toggleArchivedView);
$("conv-toggle").addEventListener("click", () => ($("conv-sidebar").hidden = !$("conv-sidebar").hidden));
$("notif-btn").addEventListener("click", toggleNotifications);
$("notif-close").addEventListener("click", () => ($("notif-drawer").hidden = true));
$("modal-close").addEventListener("click", closeModal);
$("modal-submit").addEventListener("click", (e) => { e.preventDefault(); submitModal(); });
$("modal-backdrop").addEventListener("click", (e) => { if (e.target === $("modal-backdrop")) closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("modal-backdrop").hidden) closeModal(); });

boot();
