# Project Javis

A local, offline voice AI agent for Windows.

## What it does (v2)

1. Greets you when it starts (time-aware: morning / afternoon / evening / late night).
2. Asks "What would you like to do?" and listens for a voice command.
3. Performs web searches via DuckDuckGo and reads the top 3 results aloud.
4. Loops with "Anything else?" until you say goodbye or go silent.

### Voice commands

| Say | What happens |
|-----|--------------|
| "search for python decorators" | Searches DuckDuckGo, reads top 3 snippets aloud |
| "look up weather in Paris" | Same |
| "google best python ide" | Same |
| "find me recipes for pasta" | Same |
| "close the program" / "shut down" / "terminate" | Says "Shutting down. Goodbye." and exits |
| "goodbye" / "exit" / "that's all" / "no" | Ends the session politely |
| "yes" / "yeah" / "sure" | Asks what you'd like to do next |
| (anything else) | "I don't know how to do that yet, but I will learn soon." |

## Tech stack

| Component | Engine              | Mode    | Cost  |
|-----------|---------------------|---------|-------|
| Speech-to-Text  | Vosk small-en-US  | Offline | Free  |
| Text-to-Speech  | Windows SAPI (pyttsx3) | Offline | Free |
| Audio I/O       | sounddevice / PortAudio | Local | Free |
| Web search      | DuckDuckGo Instant Answer API (requests) | Online | Free |

## Install

1. Make sure Python 3.10+ is installed and on PATH.
2. Double-click `install.bat`.

The installer will:
- Install Python dependencies
- Register Javis in `shell:startup` so it runs on every Windows login
- On first launch, Javis downloads the ~50MB Vosk model to `./models/`

## Run manually

Double-click `run.bat`.

## Uninstall

Double-click `uninstall.bat` to remove the startup entry. Then delete this folder.

## File layout

```
Project_Javis/
├── javis.py          # Voice loop, command router, main
├── search.py         # DuckDuckGo searcher (data layer)
├── requirements.txt  # Python deps
├── install.bat       # One-time installer
├── run.bat           # Manual launcher
├── run_silent.bat    # Internal: hidden launcher for startup
├── uninstall.bat     # Removes startup entry
└── models/           # Created on first run (Vosk model)
```

## Next steps (v3+)

- Wake-word detection ("Hey Javis") so the agent stays listening.
- More command handlers: open apps, run shell commands, dictate text, fetch & read a URL.
- Conversation memory across sessions.
- Plug in an LLM (local Llama or cloud) for free-form replies.
