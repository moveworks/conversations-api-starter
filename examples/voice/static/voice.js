// The voice harness, running in the browser.
//
// The OpenAI Realtime model hears the caller and speaks over WebRTC. It handles
// turn-taking, interruptions, and small talk itself, and calls the
// ask_orchestrator tool for anything that needs the company's systems. This
// file runs that hand-off:
//
//   1. On connect, ask the orchestrator who the caller is (warm-up) and give the
//      answer to the model as background, then greet.
//   2. When the model calls ask_orchestrator, POST /api/ask. The server runs the
//      Conversations API turn and streams progress lines, then the result.
//   3. While the orchestrator works, cover the wait: a first filler after a short
//      delay (skipped when the model already acknowledged), then an update when
//      the orchestrator reports new activity, or a brief reassurance when it has
//      been quiet too long. Never two responses at once.
//   4. When the result arrives, stop any filler, return the result as the tool
//      output, and let the model deliver it.
//   5. If the caller talks over a pending lookup, cancel it (a tuning option).
//
// Timing and wording come from the server (examples/voice/tuning.py and prompts/).

const $ = (id) => document.getElementById(id);
const els = { connect: $("connect"), mute: $("mute"), status: $("status"), user: $("user"), log: $("log") };
const LANES = ["caller", "voice", "orchestrator"];
const CANCELLED = {
  status: "cancelled",
  message: "The caller interrupted before the orchestrator answered. Don't bring it up unless they ask again.",
};
const UNREACHABLE = {
  status: "error",
  message: "The orchestrator couldn't be reached. Apologize briefly and suggest trying again.",
};

const state = {
  session: null, tuning: null, pc: null, dc: null, mic: null, audio: null, startedAt: 0,
  active: null, activeKind: null, pendingKind: null, queue: [], kinds: {}, lookup: null,
};

// --- log ------------------------------------------------------------------

function log(lane, text, kind) {
  if (!text) return;
  const row = document.createElement("div");
  row.className = "row";
  const seconds = state.startedAt ? (performance.now() - state.startedAt) / 1000 : 0;
  const time = document.createElement("span");
  time.className = "t";
  time.textContent = `${seconds.toFixed(1)}s`;
  row.append(time);
  for (const name of LANES) {
    const cell = document.createElement("span");
    if (name === lane) {
      cell.className = `cell ${kind || ""}`;
      if (kind && kind !== "say" && kind !== "speak") {
        const label = document.createElement("span");
        label.className = "kind";
        label.textContent = kind;
        cell.append(label);
      }
      cell.append(document.createTextNode(text));
    }
    row.append(cell);
  }
  els.log.append(row);
  row.scrollIntoView({ block: "nearest" });
}

function setStatus(text) {
  els.status.textContent = text;
}

// --- plumbing -------------------------------------------------------------

async function postJSON(path, body, signal) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    const detail = await response.json().then((b) => b.detail).catch(() => "");
    throw new Error(detail || `${path} returned ${response.status}`);
  }
  return response;
}

function send(event) {
  if (state.dc && state.dc.readyState === "open") state.dc.send(JSON.stringify(event));
}

// One response at a time: queue a response.create until the current one is done.
function createResponse(response) {
  state.queue.push(response);
  flushQueue();
}

function flushQueue() {
  if (state.active || !state.queue.length) return;
  const response = state.queue.shift();
  state.active = "pending";
  state.pendingKind = (response.metadata && response.metadata.kind) || "speak";
  send({ type: "response.create", response });
}

// --- connect --------------------------------------------------------------

async function connect() {
  els.connect.disabled = true;
  setStatus("Connecting…");
  try {
    const session = await (await postJSON("/api/session", {})).json();
    state.session = session;
    state.tuning = session.tuning;
    els.user.textContent = `Acting as ${session.user}`;

    const pc = new RTCPeerConnection();
    state.pc = pc;
    state.audio = new Audio();
    state.audio.autoplay = true;
    pc.ontrack = (event) => { state.audio.srcObject = event.streams[0]; };
    state.mic = await navigator.mediaDevices.getUserMedia({ audio: true });
    pc.addTrack(state.mic.getAudioTracks()[0], state.mic);

    const dc = pc.createDataChannel("oai-events");
    state.dc = dc;
    dc.addEventListener("message", (event) => onEvent(JSON.parse(event.data)));
    dc.addEventListener("open", onOpen);
    dc.addEventListener("close", () => setStatus("Disconnected"));

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const answer = await fetch(session.calls_url, {
      method: "POST",
      body: offer.sdp,
      headers: { Authorization: `Bearer ${session.client_secret}`, "Content-Type": "application/sdp" },
    });
    if (!answer.ok) throw new Error(`The voice model refused the connection (${answer.status}).`);
    await pc.setRemoteDescription({ type: "answer", sdp: await answer.text() });
    els.connect.textContent = "Hang up";
    els.connect.classList.remove("primary");
    els.connect.disabled = false;
    els.mute.disabled = false;
  } catch (error) {
    setStatus(error.message || String(error));
    hangUp();
  }
}

async function onOpen() {
  state.startedAt = performance.now();
  setStatus(`Connected · ${state.session.model}`);
  if (state.tuning.warmup) {
    log("voice", "Asks the orchestrator who's calling", "call");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), state.tuning.warmup_wait_ms);
    try {
      const { context } = await (await postJSON("/api/warmup", { session_id: state.session.session_id }, controller.signal)).json();
      if (context) {
        log("orchestrator", context, "result");
        send({
          type: "conversation.item.create",
          item: {
            type: "message",
            role: "system",
            content: [{ type: "input_text", text: `Background from the Moveworks orchestrator, not said by the caller:\n${context}` }],
          },
        });
      }
    } catch {
      log("voice", "Warm-up skipped; greeting without it", "progress");
    } finally {
      clearTimeout(timer);
    }
  }
  createResponse({ instructions: state.tuning.welcome });
}

function hangUp() {
  if (state.lookup) cancelLookup(state.lookup, false);
  if (state.dc) state.dc.close();
  if (state.pc) state.pc.close();
  if (state.mic) state.mic.getTracks().forEach((track) => track.stop());
  if (state.session) postJSON("/api/end", { session_id: state.session.session_id }).catch(() => {});
  Object.assign(state, { session: null, pc: null, dc: null, mic: null, active: null, queue: [], kinds: {}, lookup: null });
  els.connect.textContent = "Connect";
  els.connect.classList.add("primary");
  els.connect.disabled = false;
  els.mute.disabled = true;
  els.mute.textContent = "Mute";
}

// --- realtime events ------------------------------------------------------

function onEvent(event) {
  switch (event.type) {
    case "response.created": {
      const kind = state.active === "pending" ? state.pendingKind : "speak";
      state.active = event.response.id;
      state.activeKind = kind;
      state.kinds[event.response.id] = kind;
      break;
    }
    case "response.done":
      onResponseDone(event.response);
      break;
    case "input_audio_buffer.speech_started":
      onBargeIn();
      break;
    case "conversation.item.input_audio_transcription.completed":
      log("caller", (event.transcript || "").trim(), "say");
      break;
    case "response.output_audio_transcript.done":
      log("voice", event.transcript, state.kinds[event.response_id] || "speak");
      break;
    case "error":
      log("voice", `Realtime error: ${(event.error && event.error.message) || "unknown"}`, "error");
      if (state.active === "pending") {
        // The response we asked for was refused; don't wait for it.
        state.active = null;
        flushQueue();
      }
      break;
    default:
      break;
  }
}

function onResponseDone(response) {
  if (state.active === response.id || state.active === "pending") {
    state.active = null;
    state.activeKind = null;
  }
  const output = response.output || [];
  const spoke = output.some((item) => item.type === "message");
  for (const item of output) {
    if (item.type === "function_call" && item.name === "ask_orchestrator") startLookup(item, spoke);
  }
  flushQueue();
}

// --- the hand-off to the orchestrator ---------------------------------------

function startLookup(item, modelSpoke) {
  let args = {};
  try { args = JSON.parse(item.arguments || "{}"); } catch { args = {}; }
  if (state.lookup) cancelLookup(state.lookup, true);
  const now = performance.now();
  const lookup = {
    callId: item.call_id, query: (args.query || args.choice || "").trim(), startedAt: now, lastSpokeAt: now,
    status: "", spokenStatus: "", controller: new AbortController(), timers: [],
  };
  state.lookup = lookup;
  log("voice", `ask_orchestrator: ${lookup.query}${args.choice ? ` (picks "${args.choice}")` : ""}`, "call");

  const t = state.tuning;
  if (!modelSpoke) {
    const delay = t.upfront_filler_ms[args.expected_duration === "long" ? "long" : "quick"];
    lookup.timers.push(setTimeout(() => speakWhileWaiting(lookup, t.filler.replace("{query}", lookup.query)), delay));
  }
  lookup.timers.push(setInterval(() => progressTick(lookup), 1000));
  runLookup(lookup, args);
}

function progressTick(lookup) {
  const t = state.tuning;
  const quiet = performance.now() - lookup.lastSpokeAt;
  if (state.lookup !== lookup || quiet < t.progress_min_ms) return;
  if (lookup.status && lookup.status !== lookup.spokenStatus) {
    if (speakWhileWaiting(lookup, t.progress.replace("{status}", lookup.status))) lookup.spokenStatus = lookup.status;
  } else if (quiet >= t.progress_max_ms) {
    speakWhileWaiting(lookup, t.filler.replace("{query}", lookup.query));
  }
}

// An out-of-band response: spoken, but kept out of the conversation, so fillers
// never become part of what the model remembers.
function speakWhileWaiting(lookup, instructions) {
  if (state.lookup !== lookup || state.active || state.queue.length) return false;
  lookup.lastSpokeAt = performance.now();
  createResponse({ conversation: "none", output_modalities: ["audio"], input: [], instructions, metadata: { kind: "filler" } });
  return true;
}

async function runLookup(lookup, args) {
  try {
    const response = await postJSON(
      "/api/ask",
      { session_id: state.session.session_id, query: args.query || "", choice: args.choice || null },
      lookup.controller.signal,
    );
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newline;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (line) onLookupEvent(lookup, JSON.parse(line));
      }
    }
  } catch (error) {
    if (lookup.controller.signal.aborted) return;
    log("orchestrator", error.message || String(error), "error");
  }
  if (state.lookup === lookup) deliver(lookup, UNREACHABLE);
}

function onLookupEvent(lookup, event) {
  if (event.type === "progress") {
    lookup.status = event.text;
    log("orchestrator", event.text, "progress");
  } else if (event.type === "result") {
    deliver(lookup, event.output);
  }
}

function deliver(lookup, output) {
  if (state.lookup !== lookup) return;
  stopTimers(lookup);
  state.lookup = null;
  log("orchestrator", output.answer || output.message || output.status, output.status === "answered" ? "result" : "error");
  send({ type: "conversation.item.create", item: { type: "function_call_output", call_id: lookup.callId, output: JSON.stringify(output) } });
  if (state.activeKind === "filler") {
    // Don't make the answer wait behind a filler that's still talking.
    send({ type: "response.cancel" });
    send({ type: "output_audio_buffer.clear" });
  }
  createResponse({});
}

function onBargeIn() {
  const lookup = state.lookup;
  if (!lookup || !state.tuning.cancel_lookup_on_barge_in) return;
  if (performance.now() - lookup.startedAt < 1500) return; // the tail of the request itself
  cancelLookup(lookup, true);
}

function cancelLookup(lookup, tellModel) {
  stopTimers(lookup);
  lookup.controller.abort();
  state.lookup = null;
  if (tellModel) {
    send({ type: "conversation.item.create", item: { type: "function_call_output", call_id: lookup.callId, output: JSON.stringify(CANCELLED) } });
    log("voice", "Caller interrupted; lookup cancelled", "error");
  }
}

function stopTimers(lookup) {
  lookup.timers.forEach((timer) => { clearTimeout(timer); clearInterval(timer); });
  lookup.timers = [];
}

// --- controls -------------------------------------------------------------

els.connect.addEventListener("click", () => (state.pc ? hangUp() : connect()));
els.mute.addEventListener("click", () => {
  const track = state.mic && state.mic.getAudioTracks()[0];
  if (!track) return;
  track.enabled = !track.enabled;
  els.mute.textContent = track.enabled ? "Mute" : "Unmute";
});
