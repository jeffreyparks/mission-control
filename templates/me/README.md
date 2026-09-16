# me/

This folder is your single input location for Mission Control. Everything the
pipeline needs to know about *you* lives here. Nothing in this folder is ever
committed to git (see the repo's .gitignore) or sent anywhere except the LLM
calls the pipeline itself makes on your behalf.

| File | Required | Purpose |
|---|---|---|
| `resume.pdf` or `resume.txt` | yes | Parsed into every fit/analysis prompt |
| `profile.md` | yes | Target roles, domain expertise, tracked keywords |
| `linkedin/profile.pdf` or `linkedin/linkedin-export.zip` | no | LinkedIn profile scan |

Credentials (BlueSky app password, API keys) do NOT go here - they go in the
repo root `.env` file (copy `.env.example` to get started). Keeping secrets out
of this folder means `me/` can be zipped up, backed up, or handed to another
tool without leaking a password.

Run `uv run setup.py` to fill this folder in interactively, `uv run setup.py
--status` to see what's currently configured, or `uv run setup.py --dry-run`
to preview what a real run would scan before spending anything.
