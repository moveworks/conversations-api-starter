# Claude Code

Connect [Claude Code](https://code.claude.com/docs/en/mcp) to the [MCP server](../../README.md).

## Try it locally

1. Start the server in `dev_static_map` mode (see [Run it locally](../../README.md#run-it-locally)).
2. From a folder without code in it, start Claude Code with the example config. The token picks the roster user:

   ```bash
   MOVEWORKS_MCP_TOKEN="dev:<roster email>" claude --strict-mcp-config --mcp-config <path to repo>/mcp_server/clients/claude-code/mcp.json
   ```

3. Run `/mcp` to check that `moveworks` is connected, then ask a workplace question, such as "How do I set up VPN on my laptop?"

The tools appear as `mcp__moveworks__<tool>`. Claude chooses a tool from its description, so a workplace question can go to Moveworks without naming it in the prompt.

`mcp.json` reads the URL from `MOVEWORKS_MCP_URL` (default `http://127.0.0.1:8791/mcp`) and the token from `MOVEWORKS_MCP_TOKEN`. `--strict-mcp-config` loads only this server, so MCP servers from your own Claude Code settings don't take the question.

## Keep it

Copy the `moveworks` entry into a project's `.mcp.json`, or add it with the CLI:

```bash
claude mcp add --transport http moveworks http://127.0.0.1:8791/mcp \
  --header "Authorization: Bearer dev:<roster email>"
```

For a deployed server in `idp_exchange` mode, Claude Code must send a token from your Okta or Entra ID (for Entra ID, see the note in [Point clients at your identity provider](../../README.md#point-clients-at-your-identity-provider)). Set `MCP_OAUTH_ISSUER` on the server (see [Point clients at your identity provider](../../README.md#point-clients-at-your-identity-provider)) so Claude Code can find your identity provider, and register Claude Code as an OAuth app there. See Claude Code's MCP documentation for its redirect URI and for signing in with its `/mcp` command.

## Confirmation

Claude Code asks before it runs a tool you haven't allowed. The example below allows the two read-only tools and `ask_moveworks`, so questions run without a prompt, and leaves out `choose_moveworks_option` and `submit_moveworks_form`, so the permission prompt is the confirmation before a ticket is filed or a form is submitted. `ask_moveworks` is annotated as a write tool because the assistant can act on plain text; allow it only for assistants whose plugins confirm before they change anything, and drop it from the list to confirm every question:

```bash
MOVEWORKS_MCP_TOKEN="dev:<roster email>" claude --strict-mcp-config \
  --mcp-config <path to repo>/mcp_server/clients/claude-code/mcp.json \
  --allowedTools "mcp__moveworks__ask_moveworks,mcp__moveworks__open_moveworks_form,mcp__moveworks__get_moveworks_result"
```

In non-interactive runs (`claude -p "<question>"` with the same flags), a tool that isn't allowed is denied rather than prompted, and Claude tells the user it couldn't take the action. To see which tools ran, add `--output-format stream-json --verbose`, or check the server log for `tools/call`.
