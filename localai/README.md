# Local AI Chat

Local AI Chat connects Nicotine+ room and private chat to either an Ollama
server or llama.cpp's OpenAI-compatible server. Requests run outside the
Nicotine+ main thread and completed replies are dispatched back to it.

The plugin is stateless: every prompt is an independent request. It does not
give the model tools, filesystem access, Soulseek access, or internet access.

## Nicotine+ commands

- `/ai <prompt>` asks the configured model from your current room or private
  chat. `/askai` is an alias.
- `!ai <prompt>` lets another user ask the model only after **Allow remote
  requests** is enabled.

Remote prompts are disabled by default. They can be restricted to named rooms,
rate-limited per user, limited by prompt size, and capped by simultaneous
requests. Model replies are shortened and split into bounded chat messages.

## Ollama setup

1. Start Ollama and make sure the desired model is already installed.
2. Keep **Backend** set to `ollama`.
3. Set **Server URL** to the address Nicotine+ can reach, normally
   `http://127.0.0.1:11434` when both run on the same host.
4. Set **Model** to the installed Ollama model name, such as `llama3.2`.

The plugin posts non-streaming requests to `/api/chat`.

## llama.cpp setup

Start `llama-server` with a chat-capable model. A minimal example is:

```text
llama-server -m /models/model.gguf --host 127.0.0.1 --port 8080
```

Then set:

- **Backend:** `llamacpp`
- **Server URL:** `http://127.0.0.1:8080`
- **Model:** the model identifier accepted by that server
- **API key:** the key passed to `llama-server --api-key`, if configured

The plugin posts non-streaming requests to `/v1/chat/completions`.

## Container networking

`127.0.0.1` means the Nicotine+ container itself when Nicotine+ runs in Docker.
Use a shared Compose network and the AI service name, for example
`http://ollama:11434`, or another address reachable from that container. Avoid
exposing either AI server publicly; if remote exposure is necessary, use TLS,
authentication, firewall rules, and a reverse proxy.

## Install for local review

Copy the complete `localai` directory into the Nicotine+ user plugins directory,
restart Nicotine+, enable **Local AI Chat**, and configure its settings. No
third-party Python packages are required.
