/* Calls to this app's own server. The server rejects writes that are not
   JSON (a cross-site protection), so every POST goes through postJson. */

// FastAPI error "detail" can be a string or a list of validation objects.
export function apiDetail(body, fallback) {
  const d = body && body.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((e) => e.msg || JSON.stringify(e)).join("; ");
  return fallback;
}

// A failing proxy call can return HTML or a bare "Internal Server Error",
// so never hand a response body straight to JSON.parse.
async function readJson(resp, fallback) {
  const text = await resp.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { /* non-JSON */ }
  if (!resp.ok) {
    throw new Error(body ? apiDetail(body, fallback || `request failed (${resp.status})`)
                         : `${resp.status} ${text.slice(0, 120) || "request failed"}`);
  }
  return body;
}

export async function getJson(url) {
  return readJson(await fetch(url));
}

export function postRaw(url, body, method = "POST", signal = undefined) {
  return fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
    signal,
  });
}

export async function postJson(url, body, fallback) {
  return readJson(await postRaw(url, body), fallback);
}

export async function patchJson(url, body, fallback) {
  return readJson(await postRaw(url, body, "PATCH"), fallback);
}

// Read a server-sent event stream from a fetch response, one frame at a time.
export async function readEventStream(resp, onFrame) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const frame = parseFrame(buffer.slice(0, idx));
      buffer = buffer.slice(idx + 2);
      if (frame) onFrame(frame);
    }
  }
}

function parseFrame(raw) {
  let event = "message";
  let data = "";
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (!data) return null;
  try { return { event, data: JSON.parse(data) }; } catch { return null; }
}
