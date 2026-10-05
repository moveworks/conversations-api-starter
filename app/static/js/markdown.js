/* A small CommonMark subset: paragraphs, lists, headings, code, emphasis,
   links. Escapes first, then transforms, so API text cannot inject HTML.
   The server already converted the API's Slack markup to CommonMark. */

export function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

export function mdInline(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|\W)\*([^*\n]+)\*(?=\W|$)/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

export function renderMarkdown(el, raw) {
  const lines = escapeHtml(raw || "").split("\n");
  let html = "";
  let list = null; // "ul" | "ol"
  let liOpen = false;
  let hadBlank = false;
  let inCode = false;
  let codeBuf = [];
  const closeLi = () => { if (liOpen) { html += "</li>"; liOpen = false; } };
  const closeList = () => { closeLi(); if (list) { html += `</${list}>`; list = null; } };
  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      if (inCode) { html += `<pre><code>${codeBuf.join("\n")}</code></pre>`; codeBuf = []; }
      inCode = !inCode;
      continue;
    }
    if (inCode) { codeBuf.push(line); continue; }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (h) { closeList(); html += `<h3>${mdInline(h[2])}</h3>`; }
    else if (ul) { if (list !== "ul") { closeList(); html += "<ul>"; list = "ul"; } closeLi(); html += `<li>${mdInline(ul[1])}`; liOpen = true; }
    else if (ol) { if (list !== "ol") { closeList(); html += "<ol>"; list = "ol"; } closeLi(); html += `<li>${mdInline(ol[1])}`; liOpen = true; }
    else if (line.trim() === "") {
      // Blank lines between items must NOT end the list - closing here
      // restarts <ol> numbering at 1 for every item. Just remember it.
      hadBlank = true;
      continue;
    }
    else if (list && liOpen && (!hadBlank || /^\s{2,}\S/.test(line))) {
      // A plain line directly after a list item (lazy continuation) or an
      // indented line after a blank belongs INSIDE the item. An unindented
      // line after a blank falls through and ends the list.
      html += `<br>${mdInline(line.trim())}`;
    }
    else { closeList(); html += `<p>${mdInline(line)}</p>`; }
    hadBlank = false;
  }
  if (inCode && codeBuf.length) html += `<pre><code>${codeBuf.join("\n")}</code></pre>`;
  closeList();
  el.innerHTML = html;
}
