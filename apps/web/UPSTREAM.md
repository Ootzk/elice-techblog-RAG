# Open WebUI frontend extraction

Source: https://github.com/open-webui/open-webui/tree/b0e6098e940942281d9c83f3529ea117287a3a22
(package version 0.13.3). Original file hashes and extraction status: `upstream.json`.

Copyright (c) 2023– Open WebUI Inc. Created by Timothy Jaeryang Baek.
The original LICENSE, LICENSE_HISTORY and LICENSE_NOTICE are shipped under
`static/licenses/open-webui/` and linked in the UI. The original name and logo
remain visible. This is a modified, independent frontend, not an official Open WebUI product.

- `Textarea`, `Spinner`, and `Messages/Name` are copied unchanged.
- `Chat` preserves the upstream full-height flex conversation shell. Its state,
  session, history and request handling are replaced with the Knowledge API.
- `MessageInput` retains the extracted submit button markup/SVG and Textarea;
  file upload, voice, model selection and rich editor are removed.
- `Messages` retains the conversation section wrapper, replacing upstream tree
  navigation with an ordered list of server-owned turns.
- `ResponseMessage` retains the response wrapper/name layout; upstream tools,
  editing and model actions are replaced by verified citations and terminal jobs.
- Login, sidebar integration, API client, citations and evaluation integration are
  project code. No Open WebUI Python backend or API emulation layer is included.
- Svelte/SvelteKit/Tailwind are retained. Build dependencies are patched separately
  from the pinned upstream UI source; `package-lock.json` fixes the build.

Review upstream changes against the pinned files before updating the source pin.
Do not overwrite project-specific API/ownership semantics during an upstream update.
