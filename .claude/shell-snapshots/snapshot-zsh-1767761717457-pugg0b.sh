# Snapshot file
# Unset all aliases to avoid conflicts with functions
unalias -a 2>/dev/null || true
# Check for rg availability
if ! command -v rg >/dev/null 2>&1; then
  alias rg='/opt/homebrew/Caskroom/claude-code/2.0.76/claude --ripgrep'
fi
export PATH=/Users/brucegordon/dev/issue-orchestrator/src/issue_orchestrator/scripts\:/Users/brucegordon/.nvm/versions/node/v24.11.1/bin\:/opt/homebrew/bin\:/opt/homebrew/sbin\:/usr/local/bin\:/System/Cryptexes/App/usr/bin\:/usr/bin\:/bin\:/usr/sbin\:/sbin\:/var/run/com.apple.security.cryptexd/codex.system/bootstrap/usr/local/bin\:/var/run/com.apple.security.cryptexd/codex.system/bootstrap/usr/bin\:/var/run/com.apple.security.cryptexd/codex.system/bootstrap/usr/appleinternal/bin\:/opt/pmk/env/global/bin\:/Users/brucegordon/dev/issue-orchestrator/.venv/bin\:/Users/brucegordon/.nvm/versions/node/v24.11.1/bin\:/Users/brucegordon/.maestro/bin\:/Applications/iTerm.app/Contents/Resources/utilities
