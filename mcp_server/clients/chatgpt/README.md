# ChatGPT

Connect ChatGPT to the [MCP server](../../README.md) as a custom MCP server. OpenAI's [custom MCP server guide](https://developers.openai.com/api/docs/guides/custom-mcp-server) covers ChatGPT's side, including workspace permissions.

ChatGPT connects to remote MCP servers over HTTPS and signs users in with OAuth. It can't send a static token or custom header, so the `dev_*` modes don't work with it.

## Try it with the demo sign-in

1. Start the server behind a tunnel with the demo sign-in, as described in [Demo sign-in for OAuth-only clients](../generic/README.md#demo-sign-in-for-oauth-only-clients).
2. In ChatGPT on the web, open [Plugins](https://chatgpt.com/plugins), select the plus button, then **Add custom MCP server**. Enter a name and `https://<public host>/mcp` as the server URL, choose OAuth, and enter the demo client ID and secret as static OAuth credentials. Then select **I understand and want to continue** and **Create as a plugin**, and install the plugin.
3. In a chat, type `@`, select the plugin, and ask a workplace question. ChatGPT opens the demo sign-in page the first time; enter the passphrase.

Use a tunnel with a stable hostname; if the URL changes, add the server again with the new URL. After you change the server's tools, select **Refresh** on the plugin to pull them again.

## Confirmation

ChatGPT treats every tool not annotated read-only as a write action and asks before running it. That includes `ask_moveworks`, so ChatGPT asks before each question as well as before a button press or form submit. A user can remember the approve or deny choice for a tool within one conversation; new conversations ask again.

## With your identity provider

Set `MCP_OAUTH_ISSUER`, `MCP_OAUTH_SCOPES`, and `MCP_PUBLIC_BASE_URL` (see [Point clients at your identity provider](../../README.md#point-clients-at-your-identity-provider)). ChatGPT reads the server's discovery metadata and signs the user in with your identity provider. Also:

- ChatGPT requires the authorization server to list `S256` in `code_challenge_methods_supported`. Entra ID's discovery document doesn't list it, so ChatGPT can't sign in through Entra ID this way.
- Register the redirect URI that ChatGPT shows on the plugin's management page with your identity provider, and enter your OAuth app's client ID and secret in ChatGPT.
- ChatGPT sends a `resource` parameter naming the server's `/mcp` URL. In `idp_exchange` mode, if your identity provider uses it as the token's audience, set the Moveworks credential's Audience to match, since the server exchanges the token at Moveworks.

## Deep research

ChatGPT deep research and company knowledge use read-only `search` and `fetch` tools, which this server doesn't provide. Use it in regular chats.
