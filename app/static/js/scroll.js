import { $ } from "./dom.js";

const chatScroll = $("chat-scroll");

// Follow the bottom only while the reader is there; scrolling up to reread
// should not get yanked back down by the next update.
let stuck = true;
chatScroll.addEventListener("scroll", () => {
  stuck = chatScroll.scrollHeight - chatScroll.scrollTop - chatScroll.clientHeight < 80;
});

export function stickToBottom() {
  stuck = true;
}

export function scrollDown() {
  if (stuck) chatScroll.scrollTop = chatScroll.scrollHeight;
}
