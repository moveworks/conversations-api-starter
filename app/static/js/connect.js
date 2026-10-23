import { getJson, postJson } from "./api.js";
import { $ } from "./dom.js";
import { state } from "./state.js";
import { chatLog, resetChat } from "./chat.js";
import { dropModalReads } from "./modals.js";
import { cancelOpen, loadConversations } from "./conversations.js";
import { closeNotifications, connectNotifications } from "./notifications.js";

let env = {};

const authChoice = () => document.querySelector('input[name="auth"]:checked').value;

export async function boot() {
  const session = await getJson("/api/session");
  env = session.env || {};
  const select = $("base-url-select");
  for (const url of session.known_base_urls) select.appendChild(new Option(url, url));
  select.appendChild(new Option("Custom base URL…", "__custom__"));
  // Disabled while hidden, so a half-typed URL cannot block the form's validation.
  const showCustom = (show) => { $("base-url-custom").hidden = $("base-url-custom").disabled = !show; };
  select.addEventListener("change", () => showCustom(select.value === "__custom__"));
  // Defaults from .env are a convenience; both stay editable.
  if (env.base_url && session.known_base_urls.includes(env.base_url)) select.value = env.base_url;
  else if (env.base_url) { select.value = "__custom__"; $("base-url-custom").value = env.base_url; showCustom(true); }
  if (env.bot_name) $("bot-name").value = env.bot_name;
  if (Array.isArray(env.s2s_missing) && !env.s2s_missing.length) document.querySelector('input[name="auth"][value="s2s"]').checked = true;
  for (const radio of document.querySelectorAll('input[name="auth"]')) radio.addEventListener("change", showAuthChoice);
  showAuthChoice();
  if (session.connected) enterApp(session.bot_name, session.base_url);
}

// The server reads the S2S credential from .env; the browser only learns
// which user it acts as.
function showAuthChoice() {
  const s2s = authChoice() === "s2s";
  $("api-key-field").hidden = s2s;
  $("s2s-note").hidden = !s2s;
  $("s2s-note").textContent = (env.s2s_missing || []).length
    ? `Not configured: set ${env.s2s_missing.join(", ")} in .env and restart the app.`
    : `Acts as ${env.s2s_user}, signed with the private key in MW_S2S_PRIVATE_KEY_PATH.`;
}

export async function connect() {
  const select = $("base-url-select");
  const btn = $("connect-btn");
  const err = $("connect-error");
  err.hidden = true;
  btn.disabled = true;
  btn.textContent = "Connecting…";
  try {
    const body = await postJson("/api/connect", {
      auth: authChoice(),
      base_url: select.value === "__custom__" ? $("base-url-custom").value : select.value,
      bot_name: $("bot-name").value,
      api_key: $("api-key").value,
    }, "Connection failed.");
    $("api-key").value = "";
    enterApp(body.bot_name, body.base_url);
  } catch (e) {
    err.textContent = String(e.message || e);
    err.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = "Connect";
  }
}

function enterApp(botName, baseUrl) {
  $("connect-screen").hidden = true;
  $("app").hidden = false;
  $("bot-chip").textContent = botName;
  $("org-chip").textContent = baseUrl.replace("https://", "");
  connectNotifications();
  loadConversations();
  // The composer measured zero while hidden; re-run its autosize.
  $("chat-input").dispatchEvent(new Event("input"));
  $("chat-input").focus();
}

export async function disconnect() {
  await postJson("/api/disconnect", {});
  resetChat();
  cancelOpen();
  dropModalReads();
  closeNotifications();
  state.conversationId = null;
  chatLog.replaceChildren();
  $("chat-empty").hidden = false;
  $("app").hidden = true;
  $("connect-screen").hidden = false;
}
