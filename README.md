# dpseeker

<img src="icon.png" alt="dpseeker icon" width="50" height="50" />

dpseeker is a command-line coding assistant that turns the DeepSeek web chat into a terminal agent. You talk to it in your terminal, and it drives your machine through tools -- it can read files, write files, create folders, list directories, run shell commands, and manage git state. It works much like Claude Code or Cursor, but uses the DeepSeek model behind the scenes.

## How it works

dpseeker is a single Python script that drives a headless Chromium browser via Playwright. The browser opens chat.deepseek.com and stays logged in between runs using a persistent profile folder.

The assistant and the model communicate using a strict protocol. Every reply from the model must begin with the literal prefix `dpseekresponse` followed by a single JSON object. That object describes what the model wants to do next:

- `message`, `info`, `warning` -- print text to the user (block format between `<<<CONTENT>>>` and `<<<END>>>`).
- `ask` -- present a question with options and wait for the user's answer.
- `read_file`, `write_file`, `append_file`, `create_folder`, `tree`, `run` -- execute a tool on the user's machine.
- `git_status`, `git_diff`, `git_log`, `git_add`, `git_commit`, `git_branch` -- read or mutate git state in the project.
- `done` -- end the turn.

Write and append operations transport file content as Base64 inside the `content_b64` field. This guarantees that markdown rendering, quotes, and indentation in the chat cannot corrupt the bytes of the file being written. Large writes are split into a `write_file` chunk followed by one or more `append_file` chunks.

When the model requests a tool, dpseeker asks for confirmation (unless auto-approve is on), executes the tool, and sends the result back to the model inside a `[TOOL RESULT]` wrapper. The model then continues with the next step. This loop repeats until the model emits `done` or a conversational message.

Read results are cached by file modification time and size. If a file has not changed since the last read, the model is told `unchanged` and skips re-reading it, saving tokens and time.

Large files are attached to the chat as file uploads instead of being inlined, keeping the conversation manageable.

## Installation

```
pip install playwright
playwright install chromium
```

Optionally install `colorama` for better Windows console colors:

```
pip install colorama
```

## First run

The first time you run dpseeker, it opens a visible Chromium window so you can log into chat.deepseek.com. Once you are logged in, return to the terminal and press ENTER. Your session is stored in `~/.deepseeker` and reused on future runs.

```
python dpseeker.py
```

On startup (on a returning run) you are shown a list of your recent conversations. Pick one by number to resume it (with history replayed in the terminal), or press 0 / ENTER for a new chat.

#### Options

- `--relogin` -- wipe the profile and force a fresh login.
-  `--yes` (alias `-y`, `--dangerously-skip-permissions`) -- auto-approve every tool call. Sensitive file reads still prompt.
-  `--deepthink` -- enable DeepSeek's DeepThink (R1) mode.

## Usage

Once the chat phase starts, you get a `you>` prompt. Type your request in plain English and press ENTER. The assistant will respond and, if needed, ask to read or write files, run commands, or manipulate git. For each tool call you will be shown what it wants to do and will be asked to allow it (y=yes, n=no, a=always allow for the rest of the session). While waiting on the model a spinner shows elapsed time and received character count.

Example:

```
you> add a health check endpoint to my FastAPI app
⍬ Read(main.py)
⍬ Update(app/routes.py)
  + @app.get("/health")
  + def health():
  +     return {"ok": True}
  Allow? (y/n/a=always) › y
⍬ Bash(pytest)
  ⌔ 3 passed in 0.42s
    └ exit 0
⍺ Done.
```
Git aware example:

```
you> commit my changes

⍝ Git status
  ⌔ M! routes.py
    ⌔ exit 0
⓬ Git add(routes.py)
  Allow? (y/n/a=always) › y
⓬ Git commit(add health endpoint)
  Allow? (y/n/a=always) › y
  ⌔ [main abc123] add health endpoint
    ⌔ exit 0
₳ Done.
```

## In-chat commands

Commands are case-insensitive and must be typed on their own line.

-  `/help` -- show the command list.
-  `/cwd` -- show the current working directory.
-  /rules` -- print the active system rules sent to the model.
-  /cache` -- show how many files are in the read cache.
-  /clear` -- clear the read cache.
-  /reset` -- reset the auto-approve flag.
-  `/exit` (or `/quit`) -- quit and keep the login session.
-  `/logout` -- quit and wipe the login profile.

## Safety

Every tool call is shown to you before it runs, and writes show a diff against the current contents. Use this to catch unwanted changes before they reach disk. Paths outside the project root are flagged with a warning.

Sensitive files (`.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa`, `id_edt25519`, `id_ecdsa`, `credentials`, `secrets`, `config.json`, `*.p12`, `*.pfx`, `.npmjrc`, `.pypirc`, `.netrc`, `.git-credentials`, `auth.json`) always prompt before being read, even with `--yes active.

Avoid `--yes` when you are not sure what the assistant will do.

## Limitations

- The assistant depends on the web UI of chat.deepseek.com, so DOM selector changes on DeepSeek's side can break it until the selectors are updated.
- Tool calls time out after 120 seconds and responses after 180 seconds.
- Files larger than 200 KB cannot be read inline; larger than 20 KB are attached to the chat instead.
- A single turn is limited to 25 tool steps to prevent runaway loops.
