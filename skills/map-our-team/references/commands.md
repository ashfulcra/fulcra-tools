# Commands

Use whichever you have. In file commands, `<steward>` is the map storage-owner Fulcra ID, not a possibly different agent-contact ID. Use the contact field only for agent connections/messages.

CLI syntax below was checked against `fulcra-api` 0.1.57 on 2026-11-03. MCP calls use the connected account selected by the host; any account-selection argument is host-specific. Verify that account before writes. Command help checks do not establish that a live share or onboarding flow works.

| Do | MCP | CLI |
|---|---|---|
| Who am I | `get_user_info` or `list_shares` (`own_fulcra_userid`) | `uvx fulcra-api user-info` |
| Write a file | `write_file(path, content)` | `uvx fulcra-api file upload local.md /team-map/index.md` |
| Read your own | `read_file(path)` | `uvx fulcra-api file download /team-map/index.md -` |
| Read the steward's | `read_file(path, fulcra_userid=<steward>)` | `uvx fulcra-api file download /team-map/index.md - --user-id <steward>` |
| List the steward's | `list_files(path="/team-map/", fulcra_userid=<steward>)` | `uvx fulcra-api file list /team-map/ --user-id <steward>` |
| Find incoming maps | `list_shares(direction="incoming")` | `uvx fulcra-api share list-incoming` |
| Share the map with one person | `create_share(name="<team> team map", file_paths=["/team-map/"], with_user_ids=[<id>])` | `uvx fulcra-api share create --name "<team> team map" --file /team-map/ --user-id <id>` |
| Remove a person | `delete_share(share_id)` | `uvx fulcra-api share delete <share-id>` |
| List shares | `list_shares(direction="outgoing")` | `uvx fulcra-api share list-outgoing` |
| Notice changes | `get_data_updates(start_time, end_time, fulcra_userid=<steward>)`, under `file_changes` | `uvx fulcra-api data-updates "1 hour" --include-shared` |

Folder paths end in `/`. A folder share includes files added later, is read-only for recipients, and shares only the latest versions unless you ask for history.

Use the listing's share ID (`datashare_id` in CLI output) for deletion. A share name is a label, not a unique key or proof of its recipients and contents. After a failed or uncertain share mutation, list shares to establish what exists before repeating it.

MCP uses `fulcra_userid` for a shared source owner, not `user_id` or the CLI's `--user-id`. Some tool hosts silently ignore unknown arguments: a missing file in the active account is not a tested cross-account denial. Verify the active reader and exact owner parameter before classifying the result.
