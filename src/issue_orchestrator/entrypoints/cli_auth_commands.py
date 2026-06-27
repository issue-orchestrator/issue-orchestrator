"""Authentication and API-key CLI command handlers."""

import argparse
import getpass

from rich.console import Console

console = Console()


def cmd_auth(args: argparse.Namespace) -> int:
    """Manage GitHub authentication."""
    action = getattr(args, "auth_action", None)
    if action is None:
        console.print("[yellow]Usage: issue-orchestrator auth <store|clear>[/yellow]")
        console.print("[dim]For diagnostics, use: issue-orchestrator doctor[/dim]")
        return 1

    if action == "store":
        return _cmd_auth_store(args, console)
    if action == "clear":
        return _cmd_auth_clear(args, console)
    console.print(f"[red]Unknown auth action: {action}[/red]")
    return 1


def _cmd_auth_store(args: argparse.Namespace, console: Console) -> int:
    """Store GitHub token in OS keychain."""
    from ..execution.providers import store_keyring_token

    token = getattr(args, "token", None)
    if not token:
        try:
            token = getpass.getpass("Enter GitHub token: ")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Cancelled[/yellow]")
            return 1

    if not token or not token.strip():
        console.print("[red]Token cannot be empty[/red]")
        return 1

    try:
        store_keyring_token(token.strip())
        console.print("[green]✓ Token stored in OS keychain[/green]")
        console.print(
            "[dim]The token will be used when ISSUE_ORCH_GITHUB_TOKEN is not set.[/dim]"
        )
        return 0
    except ImportError:
        console.print("[red]Error: keyring library not installed[/red]")
        console.print("[dim]Install with: pip install keyring[/dim]")
        return 1
    except Exception as exc:
        console.print(f"[red]Failed to store token: {exc}[/red]")
        return 1


def _cmd_auth_clear(args: argparse.Namespace, console: Console) -> int:  # noqa: ARG001
    """Clear GitHub token from OS keychain."""
    from ..execution.providers import clear_keyring_token

    if clear_keyring_token():
        console.print("[green]✓ Token cleared from OS keychain[/green]")
    else:
        console.print("[yellow]No token was stored in keychain[/yellow]")
    return 0


def cmd_keys(args: argparse.Namespace) -> int:
    """Manage AI provider API keys."""
    action = getattr(args, "keys_action", None)
    if action is None:
        console.print(
            "[yellow]Usage: issue-orchestrator keys <list|set|delete>[/yellow]"
        )
        return 1

    if action == "list":
        return _cmd_keys_list(args)
    if action == "set":
        return _cmd_keys_set(args)
    if action == "delete":
        return _cmd_keys_delete(args)
    console.print(f"[red]Unknown keys action: {action}[/red]")
    return 1


def _cmd_keys_list(args: argparse.Namespace) -> int:  # noqa: ARG001
    """List stored AI provider API keys."""
    from ..infra.ai_keys import get_ai_providers, list_ai_keys

    keys = list_ai_keys()
    ai_providers = get_ai_providers()
    console.print("\n[bold]AI Provider Keys:[/bold]")
    for key_name, (masked, source) in keys.items():
        provider_info = ai_providers.get(key_name, {})
        provider_name = provider_info.get("name", key_name)
        if source == "not set":
            console.print(f"  {provider_name} ({key_name}): [dim]not configured[/dim]")
        else:
            console.print(
                f"  {provider_name} ({key_name}): {masked} [dim]({source})[/dim] ✓"
            )
    console.print()
    return 0


def _cmd_keys_set(args: argparse.Namespace) -> int:
    """Store an AI provider API key in keyring."""
    from ..infra.ai_keys import get_ai_providers, normalize_ai_key_name, store_ai_key

    key_name = normalize_ai_key_name(args.key_name)
    if not key_name:
        console.print("[red]Key name cannot be empty[/red]")
        return 1

    ai_providers = get_ai_providers()

    # Show setup help for known providers
    if key_name in ai_providers:
        info = ai_providers[key_name]
        console.print(f"\n[bold]{info['name']}[/bold]")
        setup_cmd = info.get("setup_cmd")
        if setup_cmd:
            console.print(
                f"  Run in another terminal: [cyan]{setup_cmd}[/cyan]"
            )
            console.print("  Then paste the key here.")
        else:
            console.print(f"  {info.get('setup_help', info.get('url', ''))}")
        console.print()

    # Prompt for key
    try:
        value = getpass.getpass(f"Paste your {key_name}: ")
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]Cancelled[/yellow]")
        return 1

    if not value.strip():
        console.print("[red]No key provided[/red]")
        return 1

    try:
        store_ai_key(key_name, value.strip())
        console.print(f"[green]✓ Stored {key_name} in keyring[/green]")
        return 0
    except ImportError:
        console.print("[red]Error: keyring library not installed[/red]")
        console.print("[dim]Install with: pip install keyring[/dim]")
        return 1
    except Exception as exc:
        console.print(f"[red]Failed to store key: {exc}[/red]")
        return 1


def _cmd_keys_delete(args: argparse.Namespace) -> int:
    """Delete an AI provider API key from keyring."""
    from ..infra.ai_keys import delete_ai_key, normalize_ai_key_name

    key_name = normalize_ai_key_name(args.key_name)
    if not key_name:
        console.print("[red]Key name cannot be empty[/red]")
        return 1

    if delete_ai_key(key_name):
        console.print(f"[green]✓ Removed {key_name} from keyring[/green]")
    else:
        console.print(f"[yellow]{key_name} was not in keyring[/yellow]")
    return 0
