// Collapsed by default: the answer is the point, sources are for checking it.
export function renderSources(turn, sources) {
  let box = turn.querySelector(".sources");
  if (!sources || !sources.length) {
    if (box) box.remove();
    return;
  }
  if (!box) {
    box = document.createElement("details");
    box.className = "sources";
    const list = document.createElement("div");
    list.className = "sources-list";
    box.append(document.createElement("summary"), list);
    turn.appendChild(box);
  }
  box.querySelector("summary").textContent = `Sources (${sources.length})`;
  box.querySelector(".sources-list").replaceChildren(...sources.map(sourceItem));
}

function sourceItem(source) {
  const item = document.createElement(source.url ? "a" : "div");
  item.className = "source";
  if (source.url) {
    item.href = source.url;
    item.target = "_blank";
    item.rel = "noopener";
  }
  const title = document.createElement("div");
  title.className = "source-title";
  title.textContent = source.title;
  item.appendChild(title);
  if (source.snippet) {
    const snippet = document.createElement("div");
    snippet.className = "source-snippet";
    snippet.textContent = source.snippet;
    item.appendChild(snippet);
  }
  return item;
}
