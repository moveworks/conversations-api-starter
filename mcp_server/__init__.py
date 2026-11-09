"""Example MCP server around the Moveworks Conversations API.

An MCP client (Claude Code, ChatGPT, the Gemini app, Gemini CLI, or any client that speaks
StreamableHTTP) calls this server's tools; the server calls the Conversations API as
the signed-in user. You host and operate it; Moveworks does not run an MCP server for it.
"""
