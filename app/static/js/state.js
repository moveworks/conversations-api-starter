export const state = {
  conversationId: null,
  // Message ids already on screen: the same reply can reach the thread as an
  // event and as a late history read.
  shownMessageIds: new Set(),
};
