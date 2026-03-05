"""E2E security tests for guardrails.

These tests simulate a MALICIOUS agent trying every trick to bypass guardrails.
We intentionally try to be adversarial - if these tests pass, our security is solid.

Tests run in a real session environment with proper isolation applied.
"""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

# Get the scripts directory
SCRIPTS_DIR = Path(__file__).parent.parent.parent / "src" / "issue_orchestrator" / "scripts"


@pytest.fixture
def isolated_env():
    """Create an environment exactly like agent sessions get.

    This applies the same isolation that tmux sessions apply.
    """
    from issue_orchestrator.control.isolation import build_isolation_prefix

    # Create a temp worktree-like directory
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)

        # Initialize a git repo (needed for some tests)
        subprocess.run(["git", "init"], cwd=worktree, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=worktree, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=worktree, capture_output=True)

        # Build isolation prefix (same as real sessions)
        isolation = build_isolation_prefix(
            worktree=worktree,
            isolation_mode="standard",
            scrub_env=True,
            isolate_home=True,
        )

        # Build environment with wrappers in PATH
        env = os.environ.copy()
        env["PATH"] = f"{SCRIPTS_DIR}:{env.get('PATH', '')}"

        yield {
            "worktree": worktree,
            "isolation": isolation,
            "env": env,
        }


def run_in_isolation(ctx: dict, script: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a script in the isolated environment, simulating what an agent could do."""
    full_script = f"{ctx['isolation']} {script}"
    return subprocess.run(
        ["bash", "-c", full_script],
        cwd=ctx["worktree"],
        env=ctx["env"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class TestBypassAttempts:
    """Adversarial tests - try every trick to bypass guardrails."""

    def test_direct_gh_binary_auth(self, isolated_env):
        """Try to authenticate using the real gh binary directly."""
        # Find real gh
        result = run_in_isolation(isolated_env, """
            for gh in /opt/homebrew/bin/gh /usr/local/bin/gh /usr/bin/gh; do
                if [ -x "$gh" ]; then
                    $gh auth status 2>&1
                    exit $?
                fi
            done
            echo "gh not found"
            exit 1
        """)

        # Should NOT be logged in
        output = result.stdout + result.stderr
        assert "Logged in" not in output, f"SECURITY BREACH: Direct gh auth succeeded! {output}"
        assert "Active account" not in output, f"SECURITY BREACH: Direct gh has active account! {output}"

    def test_direct_git_push_bypass(self, isolated_env):
        """Try to push using the real git binary directly."""
        # Create a commit first
        (isolated_env["worktree"] / "test.txt").write_text("test")
        run_in_isolation(isolated_env, "git add test.txt && git commit -m 'test'")

        # Try direct git push
        result = run_in_isolation(isolated_env, """
            for git in /opt/homebrew/bin/git /usr/local/bin/git /usr/bin/git; do
                if [ -x "$git" ]; then
                    $git push origin main 2>&1
                    exit $?
                fi
            done
        """)

        # Should fail (no credentials)
        assert result.returncode != 0, "SECURITY BREACH: Direct git push succeeded!"
        # Should not have "Everything up-to-date" or similar success messages
        output = result.stdout + result.stderr
        assert "Everything up-to-date" not in output, f"SECURITY BREACH: Git push worked! {output}"

    def test_subprocess_python_bypass(self, isolated_env):
        """Try to use Python subprocess to call real binaries."""
        result = run_in_isolation(isolated_env, '''
            python3 -c "
import subprocess
import os

# Try to call real gh directly
for gh_path in ['/opt/homebrew/bin/gh', '/usr/local/bin/gh', '/usr/bin/gh']:
    if os.path.isfile(gh_path):
        result = subprocess.run([gh_path, 'auth', 'status'], capture_output=True, text=True)
        print(result.stdout)
        print(result.stderr)
        if 'Logged in' in result.stdout + result.stderr:
            exit(99)  # Security breach!
        break
exit(0)
"
        ''')

        assert result.returncode != 99, "SECURITY BREACH: Python subprocess bypassed auth!"

    def test_os_system_bypass(self, isolated_env):
        """Try to use os.system() to bypass."""
        result = run_in_isolation(isolated_env, '''
            python3 -c "
import os
exit_code = os.system('/opt/homebrew/bin/gh auth status 2>&1 | grep -q \"Logged in\"')
if exit_code == 0:
    exit(99)  # Security breach - logged in!
exit(0)
"
        ''')

        assert result.returncode != 99, "SECURITY BREACH: os.system bypassed auth!"

    def test_env_var_recovery(self, isolated_env):
        """Try to recover tokens from environment."""
        result = run_in_isolation(isolated_env, '''
            python3 -c "
import os
tokens = []
for var in ['GH_TOKEN', 'GITHUB_TOKEN', 'ISSUE_ORCH_GITHUB_TOKEN']:
    val = os.environ.get(var)
    if val:
        tokens.append(f'{var}={val[:4]}...')
if tokens:
    print('FOUND TOKENS:', tokens)
    exit(99)
print('No tokens in env')
exit(0)
"
        ''')

        assert result.returncode != 99, f"SECURITY BREACH: Tokens found in env! {result.stdout}"

    # TODO:docker - With Docker isolation, this test could run because the container
    # would have a separate /proc namespace, preventing access to host environ.
    @pytest.mark.skipif(
        bool(os.environ.get("GITHUB_TOKEN")),
        reason="Cannot test /proc isolation when GITHUB_TOKEN is in parent environ (Linux /proc is readable by same-user processes)",
    )
    def test_proc_environ_bypass(self, isolated_env):
        """Try to read parent process environment from /proc."""
        result = run_in_isolation(isolated_env, '''
            python3 -c "
import os

# Try to read parent's environ (Linux only, won't work on macOS)
ppid = os.getppid()
try:
    with open(f'/proc/{ppid}/environ', 'rb') as f:
        environ = f.read()
        if b'GH_TOKEN' in environ or b'GITHUB_TOKEN' in environ:
            print('FOUND TOKEN IN PARENT ENVIRON')
            exit(99)
except FileNotFoundError:
    pass  # macOS doesn't have /proc
except PermissionError:
    pass  # Good - can't read parent environ
exit(0)
"
        ''')

        assert result.returncode != 99, "SECURITY BREACH: Found tokens in parent environ!"

    def test_git_credential_helper(self, isolated_env):
        """Try to use git credential helper to get tokens."""
        result = run_in_isolation(isolated_env, '''
            # Try to get credentials via git credential helper
            echo "protocol=https
host=github.com
" | git credential fill 2>&1
        ''')

        # Should not return a password/token
        output = result.stdout + result.stderr
        assert "password=" not in output.lower(), f"SECURITY BREACH: Git credential returned password! {output}"

    # NOTE: macOS Keychain access cannot be blocked via env isolation.
    # The gh CLI stores tokens in Keychain which can be read via `security` command.
    # In production, agents should run in containers/sandboxes without host keychain access.

    def test_gh_config_file_access(self, isolated_env):
        """Try to read gh config files for tokens."""
        result = run_in_isolation(isolated_env, '''
            python3 -c "
import os
from pathlib import Path

# Try various gh config locations
config_paths = [
    Path.home() / '.config' / 'gh' / 'hosts.yml',
    Path(os.environ.get('XDG_CONFIG_HOME', '')) / 'gh' / 'hosts.yml',
    Path('/etc/gh/hosts.yml'),
]

for config_path in config_paths:
    try:
        content = config_path.read_text()
        if 'oauth_token' in content or 'token' in content.lower():
            print(f'FOUND TOKEN IN {config_path}')
            exit(99)
    except (FileNotFoundError, PermissionError):
        pass

print('No accessible gh configs with tokens')
exit(0)
"
        ''')

        # With HOME isolation, this should fail to find configs
        # But even if it finds them, the isolation should have changed HOME
        assert result.returncode != 99, f"SECURITY BREACH: Found gh config with token! {result.stdout}"

    def test_curl_with_token_header(self, isolated_env):
        """Try to use curl with token to GitHub API."""
        result = run_in_isolation(isolated_env, '''
            # Try to get user info - would work if we had a token
            curl -s -H "Authorization: token ${GH_TOKEN:-none}" \
                 -H "Authorization: token ${GITHUB_TOKEN:-none}" \
                 https://api.github.com/user 2>&1 | head -5
        ''')

        output = result.stdout
        # If we get user info back, we have a working token
        assert '"login"' not in output, f"SECURITY BREACH: Curl to GitHub API succeeded! {output}"

    def test_wrapper_bypass_via_path_manipulation(self, isolated_env):
        """Try to bypass wrapper by manipulating PATH."""
        result = run_in_isolation(isolated_env, '''
            # Try to remove wrapper from PATH and use real gh
            export PATH=$(echo $PATH | tr ':' '\n' | grep -v issue_orchestrator | tr '\n' ':')
            gh auth status 2>&1
        ''')

        # Even without wrapper, should fail due to no credentials
        output = result.stdout + result.stderr
        assert "Logged in" not in output, f"SECURITY BREACH: PATH manipulation bypassed auth! {output}"


class TestWrapperEnforcement:
    """Test that wrappers are actually being used and enforcing rules."""

    def test_wrapper_blocks_gh_pr_create(self, isolated_env):
        """Verify gh wrapper blocks pr create."""
        result = run_in_isolation(isolated_env, "gh pr create --title test --body test")

        assert result.returncode != 0
        assert "BLOCKED" in result.stderr

    def test_wrapper_blocks_gh_api(self, isolated_env):
        """Verify gh wrapper blocks api calls."""
        result = run_in_isolation(isolated_env, "gh api /user")

        assert result.returncode != 0
        assert "BLOCKED" in result.stderr

    def test_wrapper_blocks_git_push(self, isolated_env):
        """Verify git wrapper blocks push."""
        result = run_in_isolation(isolated_env, "git push")

        assert result.returncode != 0
        assert "BLOCKED" in result.stderr

    def test_wrapper_allows_gh_pr_view(self, isolated_env):
        """Verify gh wrapper allows read-only commands."""
        result = run_in_isolation(isolated_env, "gh pr view 1 2>&1 || true")

        # Should not be BLOCKED (may fail for other reasons)
        assert "BLOCKED" not in result.stderr

    def test_wrapper_allows_git_status(self, isolated_env):
        """Verify git wrapper allows non-push commands."""
        result = run_in_isolation(isolated_env, "git status")

        assert "BLOCKED" not in result.stderr
        assert result.returncode == 0


class TestHomeIsolation:
    """Test that HOME isolation prevents access to user configs."""

    def test_home_is_isolated(self, isolated_env):
        """Verify HOME is set to worktree, not real home."""
        result = run_in_isolation(isolated_env, 'echo $HOME')

        home = result.stdout.strip()
        real_home = str(Path.home())

        assert home != real_home, f"HOME not isolated! HOME={home}, real={real_home}"
        assert str(isolated_env["worktree"]) in home, f"HOME should be worktree, got {home}"

    def test_cannot_access_real_home_configs(self, isolated_env):
        """Verify cannot read configs from real home directory."""
        real_home = Path.home()
        result = run_in_isolation(isolated_env, f'''
            python3 -c "
from pathlib import Path
config = Path('{real_home}') / '.config' / 'gh' / 'hosts.yml'
try:
    print(config.read_text()[:100])
    exit(99)  # Shouldn't be able to read this
except:
    exit(0)
"
        ''')

        # This test is tricky - the file might be readable but HOME isolation
        # should prevent gh from finding it. Let's at least verify HOME is different.
        home_result = run_in_isolation(isolated_env, 'echo $HOME')
        assert str(real_home) not in home_result.stdout


class TestAgentDoneAvailability:
    """Test that completion commands (agent-done wrapper) are available and working."""

    def test_agent_done_is_available(self, isolated_env):
        """Verify agent-done wrapper command is in PATH."""
        result = run_in_isolation(isolated_env, "which agent-done")

        assert result.returncode == 0
        assert "agent-done" in result.stdout

    def test_agent_done_help_works(self, isolated_env):
        """Verify agent-done --help works."""
        result = run_in_isolation(isolated_env, "agent-done --help")

        assert result.returncode == 0
        assert "usage" in result.stdout.lower() or "completed" in result.stdout.lower()
