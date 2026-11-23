# User agents

A Jarvis user can build up to 5 agents of their own on the Agents page. Each agent
is one `Jarvis User Agent` row: a name, instructions, the tools the owner picked,
an optional shell, and an optional schedule.

Everything is off unless `Jarvis Settings.enable_user_agents` is on. With it off,
every endpoint in `jarvis.chat.user_agents_api` refuses (`list_user_agents` answers
`enabled: false` so the page can hide itself), agent chats cannot send, tool calls
from an agent session are refused, and the schedule sweep does nothing. The
control plane must also allow user agents for the workspace (tenant field
`user_agents_enabled`), and the runtime must be 2026.11.0 or newer.

## Rules (v1)

- An agent runs as its owner: the owner's ERP permissions apply to every tool call.
- Only the owner can chat with, run, edit or delete it. System Managers can delete
  one from Desk but cannot edit it. There is no sharing: a chat with an agent cannot
  be shared.
- Administrator and Guest cannot own an agent.
- 5 agents per owner, 20 per workspace (the runtime limit).
- Instructions are at most 12,000 characters. Tools are picked from a fixed list
  (`list_pickable_tools`): permission-checked reads and explicit writes, plus
  `call_connector` and `list_connector_actions`. Private memory, delegate-only
  sources and assistant authoring tools cannot be picked.
- An agent cannot change itself: a write to `Jarvis User Agent` from inside a tool
  call is refused.

## How a change reaches the runtime

1. A save that changes the name, description, instructions, tools, shell or
   enabled state marks the row Pending and queues one background job.
2. The job sends the full set of enabled agents through admin
   (`push_user_agents`) to the fleet, which adds, updates and removes agents to
   match. It sends again while the set changes under it.
3. The outcome is written on the rows: Synced, or a code with a sentence.
   `apply_in_progress`, rate limits and an unreachable host leave the row Pending
   (use Retry sync); any other refusal marks it Failed.

An agent can chat or run only while it is enabled and Synced: an edit, a
re-enable or a failed push makes it wait until its current content is on the
runtime. A failed push only touches the rows it carried a change for.

## Chats

`start_user_agent_chat` opens a new conversation bound to the agent. Its session
key is `agent:<agent_id>:dashboard:jarvis-chat-<user>-<ms>`, and the conversation
stores the agent id in `user_agent`, so a session minted again later (idle
reclaim, re-pairing) still addresses the same agent. Messages go through the
normal `send_message`. `get_conversation` and `list_conversations` carry the agent's
name as `user_agent_name` (owner only, empty for a normal chat or a deleted agent), and
the chat header shows it.

Every tool call from such a session is checked in `jarvis.api.call_tool`: the
agent must exist, be enabled, belong to the session's user, and have picked the
tool. Other sessions are not affected.

## Web browser

An agent can also get its own web browser. The option exists only while both
`Jarvis Settings.enable_user_agents` and `Jarvis Settings.enable_user_agent_browser`
are on, and the runtime host supports it.

- The owner turns on "Can use a web browser" and picks what it can open: "Listed
  sites only" (1 to 30 sites, one per line; a site's subdomains are included) or
  "Any public site". Pasted addresses are cleaned up: the scheme, path, query and
  port are dropped and the rest is lower-cased. A line that is still not a site
  is refused, and the message quotes it.
- At most 3 enabled agents per workspace can have a browser, across all owners.
- A browser change waits for the sync like any other edit: the agent is not ready
  until the runtime holds the new setting. Switching the site option on or off
  sends every browser agent again.
- The push carries the browser only while the switch is on and the agent has it.

Risk, in plain words: a web page the agent opens can trick it into sending data it
read (ERP data included, since reads are not confirmed) to a site it is allowed to
open. Listed sites reduce this; "Any public site" does not. Downloads and uploads
are off in this version. The browser runs in its own container with its inner
sandbox off, so the container is the wall.

## Schedule

A five-minute sweep starts each due agent as its owner, in a new bound chat, with
the scheduled prompt as a background turn. It fails closed for Administrator,
Guest, disabled owners and owners without Jarvis access (the schedule is switched
off). A run that does not start stores a code and a sentence on the row and
notifies the owner. A missed slot is not retried; the agent runs at its next time.
Run now does the same on demand, without moving the schedule.

## Not in v1

Downloads and uploads in the browser, sharing an agent, per-agent connector scope, native memory,
sub-agents, the runtime's own scheduler for ERP work, and more than two live
marketplace agents.
