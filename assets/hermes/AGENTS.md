# Onboarding directive

When a session starts, your first action is to run the `fulcra-onboarding`
skill (read its SKILL.md and follow it). The skill greets the user; you do not
add a separate greeting.

To authenticate Fulcra, run this EXACT command (the wrapper is important):

    timeout 12 uv tool run fulcra-api auth login 2>&1 || true

It prints an authorization URL and a device code, then returns. Do NOT run a
bare `uv tool run fulcra-api auth login` without the `timeout … 2>&1 || true`
wrapper — unwrapped it blocks indefinitely and the URL never reaches the user.
Present the URL and the code to the user in chat and tell them to open the URL
in their own browser to create a new Fulcra account or sign in. Then poll
`uv tool run fulcra-api user-info` until it succeeds before continuing. Never
ask the user for a Fulcra token; the browser device flow is the only auth path.
