"""Quota lanes are independent, and billing decides whether they exist (#7253).

The invariant this whole change exists to create: exhausting one metered pool
must not close the door on a pool it never touched. Before lanes, the circuit
keyed on the provider name, so a Fable quota trip read as "claude-code is down"
and stopped Opus agents whose own capacity was untouched.

These are the tests that would have caught that, so they assert the *behaviour*
at the circuit boundary rather than the shape of the key.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from issue_orchestrator.domain.provider_lane import BillingMode, ProviderLane
from issue_orchestrator.execution.agent_runner_providers import (
    ClaudeCodeProvider,
    CodexProvider,
    DeepSeekProvider,
)
from issue_orchestrator.execution.provider_readiness_probe import (
    CLIProviderReadinessProbe,
)
from issue_orchestrator.ports.provider_readiness import ProviderEntitlement

SUBSCRIPTION_CLAUDE = (
    '{"loggedIn": true, "authMethod": "claude.ai", '
    '"apiProvider": "firstParty", "subscriptionType": "max"}'
)
API_KEY_CLAUDE = (
    '{"loggedIn": true, "authMethod": "apiKey", '
    '"apiProvider": "apiKey", "subscriptionType": ""}'
)
CHATGPT_CODEX = "Logged in using ChatGPT"
API_KEY_CODEX = "Logged in using an API key"


class _Result:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode
        self.timed_out = False


class _Runner:
    """CommandRunner returning a canned answer per executable."""

    def __init__(self, by_executable: dict[str, str]) -> None:
        self._by_executable = by_executable
        self.calls: list[str] = []

    def run(self, argv, timeout_seconds=None):  # noqa: ANN001 - test double
        self.calls.append(argv[0])
        return _Result(self._by_executable.get(argv[0], ""))



def _installed_probe(runner: "_Runner") -> CLIProviderReadinessProbe:
    """A probe whose adapters report their CLI as installed.

    Lane splitting depends on billing, billing comes from the credential probe,
    and the probe short-circuits to NOT_INSTALLED when the CLI is absent from
    PATH. Resolving real adapters would therefore make these tests assert
    whatever happens to be installed on the host: they passed on a developer
    machine with claude and codex present and collapsed every lane to its bare
    provider in CI, where neither is. The adapter's own availability check is
    covered by its readiness tests; what these assert is lane identity, so the
    CLI's presence is injected rather than inherited from the environment.
    """
    adapters = {
        "claude-code": ClaudeCodeProvider,
        "codex": CodexProvider,
        "deepseek": DeepSeekProvider,
    }

    def resolve(name: str):
        if name not in adapters:
            raise ValueError(f"Unknown provider: {name!r}")
        adapter = adapters[name]()
        adapter.is_available = lambda: True  # type: ignore[method-assign]
        return adapter

    return CLIProviderReadinessProbe(runner, resolve_provider=resolve)


# ---------------------------------------------------------------------------
# Lane identity
# ---------------------------------------------------------------------------


class TestASeparatelyMeteredModelGetsItsOwnLane:
    """Fable and Spark bill against their own meters, so they are their own lanes."""

    @pytest.mark.parametrize(
        ("provider", "main_model", "metered_model", "expected"),
        [
            (ClaudeCodeProvider(), "opus", "fable", "claude-code:fable"),
            (CodexProvider(), "gpt-6-astra", "gpt-5.3-codex-spark", "codex:spark"),
        ],
    )
    def test_the_sub_metered_model_does_not_share_the_main_lane(
        self, provider, main_model, metered_model, expected
    ):
        prepaid = ProviderEntitlement(billing=BillingMode.PREPAID)

        main = provider.lane_for(main_model, prepaid)
        metered = provider.lane_for(metered_model, prepaid)

        assert main.key == provider.name
        assert metered.key == expected
        assert main.key != metered.key

    def test_a_lane_without_a_sub_meter_keeps_the_bare_provider_name(self):
        """Pre-lane circuit rows must keep their meaning, not be orphaned."""
        lane = ClaudeCodeProvider().lane_for(
            "opus", ProviderEntitlement(billing=BillingMode.PREPAID)
        )

        assert lane.key == "claude-code"


class TestMeteredBillingHasNoSubMeters:
    """An API-key account buys one pool of currency; splitting it invents independence."""

    @pytest.mark.parametrize(
        ("provider", "metered_model"),
        [
            (ClaudeCodeProvider(), "fable"),
            (CodexProvider(), "gpt-5.3-codex-spark"),
        ],
    )
    def test_a_metered_account_collapses_every_model_onto_one_lane(
        self, provider, metered_model
    ):
        metered = ProviderEntitlement(billing=BillingMode.METERED)

        assert provider.lane_for(metered_model, metered).key == provider.name

    def test_a_metered_lane_cannot_declare_a_sub_meter(self):
        """The type refuses the combination rather than trusting call sites.

        A split metered lane would let an exhausted balance keep launching work
        on a "different" lane backed by the same empty balance.
        """
        with pytest.raises(ValueError, match="metered billing has no sub-meters"):
            ProviderLane(
                provider="claude-code", meter="fable", billing=BillingMode.METERED
            )

    def test_undetermined_billing_resolves_to_metered(self):
        """Under-using a paid lane is cheap; draining a card is not."""
        undetermined = ProviderEntitlement()

        assert undetermined.determined is False
        assert undetermined.effective_billing is BillingMode.METERED
        assert ClaudeCodeProvider().lane_for("fable", undetermined).key == "claude-code"


# ---------------------------------------------------------------------------
# Billing observed at the probe
# ---------------------------------------------------------------------------


class TestBillingIsReadFromTheAccountNotTheProvider:
    """The same CLI is prepaid under subscription auth and metered under an API key."""

    @pytest.mark.parametrize(
        ("provider", "output", "expected"),
        [
            (ClaudeCodeProvider(), SUBSCRIPTION_CLAUDE, BillingMode.PREPAID),
            (ClaudeCodeProvider(), API_KEY_CLAUDE, BillingMode.METERED),
            (CodexProvider(), CHATGPT_CODEX, BillingMode.PREPAID),
            (CodexProvider(), API_KEY_CODEX, BillingMode.METERED),
        ],
    )
    def test_the_auth_probe_reports_how_the_account_pays(
        self, provider, output, expected
    ):
        assert provider.read_entitlement(output, 0).billing is expected

    def test_an_unrecognised_credential_stays_undetermined(self):
        """Never guess prepaid: the fail-safe direction cannot spend money."""
        entitlement = CodexProvider().read_entitlement("something unfamiliar", 0)

        assert entitlement.billing is None
        assert entitlement.effective_billing is BillingMode.METERED

    def test_deepseek_is_metered_regardless_of_output(self):
        """DeepSeek sells no subscription, so billing is a product fact."""
        assert (
            DeepSeekProvider().read_entitlement("anything at all", 0).billing
            is BillingMode.METERED
        )


class TestLaneResolutionProbesRatherThanGuessing:
    def test_a_cold_cache_still_splits_a_sub_metered_lane(self):
        """Resolving from a cold cache would silently collapse the lanes.

        Billing decides whether sub-meters exist, so a lane resolved before any
        credential sample would report undetermined billing and undo the very
        separation this exists to create — on the planning path, which is where
        most lane resolution happens.
        """
        runner = _Runner({"claude": SUBSCRIPTION_CLAUDE})
        probe = _installed_probe(runner)

        assert probe.lane_for("claude-code", "fable").key == "claude-code:fable"

    def test_repeat_resolution_reuses_one_credential_sample(self):
        """Planning resolves a lane per candidate launch; probing each is waste."""
        runner = _Runner({"codex": CHATGPT_CODEX})
        probe = _installed_probe(runner)

        for _ in range(5):
            probe.lane_for("codex", "gpt-5.3-codex-spark")

        assert runner.calls == ["codex"]

    def test_an_unknown_provider_yields_a_usable_single_lane(self):
        """Choosing a circuit row must not raise on an unrecognised name."""
        probe = _installed_probe(_Runner({}))

        assert probe.lane_for("not-a-provider", "whatever").key == "not-a-provider"


# ---------------------------------------------------------------------------
# The circuit keeps lanes independent
# ---------------------------------------------------------------------------


class _NullEvents:
    """EventSink that records nothing; these tests assert circuit state."""

    def publish(self, event) -> None:  # noqa: ANN001 - test double
        return None


@pytest.fixture
def circuit():
    from issue_orchestrator.control.provider_resilience import (
        ProviderResilienceManager,
    )
    from issue_orchestrator.infra.config_models import (
        ProviderCircuitBreakerConfig,
        ProviderResilienceConfig,
    )
    from issue_orchestrator.ports.provider_resilience import (
        InMemoryProviderCircuitStore,
    )

    return ProviderResilienceManager(
        config=ProviderResilienceConfig(
            circuit_breaker=ProviderCircuitBreakerConfig()
        ),
        store=InMemoryProviderCircuitStore(),
        events=_NullEvents(),
    )


class TestExhaustingOneLaneLeavesTheOthersOpen:
    """The regression this change exists to prevent."""

    def test_a_fable_outage_does_not_park_the_opus_lane(self, circuit):
        now = datetime.now(timezone.utc)

        for _ in range(10):
            circuit.record_quota_failure(
                "claude-code:fable", error_summary="fable weekly limit", now=now
            )

        assert circuit.is_open("claude-code:fable", now) is True
        assert circuit.is_open("claude-code", now) is False

    def test_a_spark_outage_does_not_park_the_main_codex_lane(self, circuit):
        now = datetime.now(timezone.utc)

        for _ in range(10):
            circuit.record_quota_failure(
                "codex:spark", error_summary="spark weekly limit", now=now
            )

        assert circuit.is_open("codex:spark", now) is True
        assert circuit.is_open("codex", now) is False

    def test_a_deepseek_outage_does_not_park_anthropic_capacity(self, circuit):
        """A different vendor's balance says nothing about a subscription."""
        now = datetime.now(timezone.utc)

        for _ in range(10):
            circuit.record_quota_failure(
                "deepseek", error_summary="insufficient balance", now=now
            )

        assert circuit.is_open("deepseek", now) is True
        assert circuit.is_open("claude-code", now) is False
        assert circuit.is_open("claude-code:fable", now) is False
        assert circuit.is_open("codex", now) is False

    def test_the_main_lane_tripping_leaves_the_sub_meter_usable(self, circuit):
        """Independence runs both ways, which is the point of separate meters."""
        now = datetime.now(timezone.utc)

        for _ in range(10):
            circuit.record_quota_failure(
                "codex", error_summary="weekly limit", now=now
            )

        assert circuit.is_open("codex", now) is True
        assert circuit.is_open("codex:spark", now) is False


class TestBillingModeDescribesHowExhaustionHeals:
    def test_prepaid_capacity_returns_without_a_human(self):
        """A weekly subscription meter refills on its own."""
        assert BillingMode.PREPAID.heals_on_timer is True
        assert (
            ProviderLane("codex", "spark", BillingMode.PREPAID).heals_on_timer is True
        )

    def test_a_metered_balance_only_a_person_can_restore(self):
        """No window returns anything; someone has to top the balance up."""
        assert BillingMode.METERED.heals_on_timer is False
        assert ProviderLane("deepseek", None, BillingMode.METERED).heals_on_timer is False


class TestLaneRejectsNonsense:
    def test_a_lane_requires_a_provider(self):
        with pytest.raises(ValueError, match="requires a provider name"):
            ProviderLane(provider="")

    def test_an_empty_meter_is_not_the_same_as_no_meter(self):
        """`""` would render as `provider:` and silently mint a second row."""
        with pytest.raises(ValueError, match="must be a name or None"):
            ProviderLane(
                provider="codex", meter="", billing=BillingMode.PREPAID
            )


# ---------------------------------------------------------------------------
# Credentials never reach argv
# ---------------------------------------------------------------------------


class TestProviderCredentialsStayOutOfArgv:
    """`ps` is world-readable; a key in the command string leaks fleet-wide."""

    def test_the_deepseek_key_is_environment_not_command_line(self):
        provider = DeepSeekProvider()
        secret = "sk-not-a-real-deepseek-key"

        env = provider.session_env(secrets={provider.API_KEY_NAME: secret})
        argv = provider.build_command("do the work", "deepseek-v4-pro")

        assert env["ANTHROPIC_API_KEY"] == secret
        assert env["ANTHROPIC_BASE_URL"] == DeepSeekProvider.BASE_URL
        assert not any(secret in str(arg) for arg in argv)

    def test_the_real_context_window_is_declared(self):
        """Claude Code assumes 200k for slugs it does not recognise.

        Every DeepSeek slug is unrecognised, and DeepSeek serves 1M. Without
        this the agent auto-compacts at a fifth of its real window, silently
        discarding context part-way through every long session.
        """
        env = DeepSeekProvider().session_env(secrets={})

        assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "1000000"

    def test_a_missing_key_yields_no_empty_credential(self):
        """An empty key would authenticate as nobody and fail confusingly."""
        env = DeepSeekProvider().session_env(secrets={})

        assert "ANTHROPIC_API_KEY" not in env
        assert env["ANTHROPIC_BASE_URL"] == DeepSeekProvider.BASE_URL

    def test_only_declared_secrets_are_requested(self):
        """A codex session has no business receiving a DeepSeek key."""
        assert ClaudeCodeProvider().required_secret_names == ()
        assert CodexProvider().required_secret_names == ()
        assert DeepSeekProvider().required_secret_names == ("DEEPSEEK_API_KEY",)

    def test_credentials_are_resolved_per_provider(self, monkeypatch):
        from issue_orchestrator.execution import provider_credentials_adapter

        monkeypatch.setattr(
            "issue_orchestrator.infra.ai_keys.read_ai_key",
            lambda name: "sk-secret" if name == "DEEPSEEK_API_KEY" else None,
        )
        resolver = provider_credentials_adapter.KeyringProviderCredentials()

        assert resolver.session_env("codex") == {}
        assert resolver.session_env("claude-code") == {}
        assert resolver.session_env("deepseek")["ANTHROPIC_API_KEY"] == "sk-secret"


class TestDeepSeekReadinessDoesNotAnswerForAnthropic:
    """`claude auth status` reports the Anthropic login, which is irrelevant here."""

    def test_a_healthy_anthropic_login_is_not_deepseek_readiness(self, monkeypatch):
        monkeypatch.setattr(
            "issue_orchestrator.infra.ai_keys.read_ai_key", lambda name: None
        )
        provider = DeepSeekProvider()
        monkeypatch.setattr(provider, "is_available", lambda: True)

        readiness = provider.check_readiness(_Runner({"claude": SUBSCRIPTION_CLAUDE}))

        assert readiness.authenticated is False
        assert readiness.human_fixable is True
        assert "issue-orchestrator keys set deepseek" in readiness.detail

    def test_a_configured_key_reports_ready_and_metered(self, monkeypatch):
        monkeypatch.setattr(
            "issue_orchestrator.infra.ai_keys.read_ai_key", lambda name: "sk-secret"
        )
        provider = DeepSeekProvider()
        monkeypatch.setattr(provider, "is_available", lambda: True)

        readiness = provider.check_readiness(_Runner({}))

        assert readiness.authenticated is True
        assert readiness.entitlement.billing is BillingMode.METERED

    def test_the_readiness_probe_spends_no_tokens(self, monkeypatch):
        """Credential state is local; a probe must never bill the operator."""
        monkeypatch.setattr(
            "issue_orchestrator.infra.ai_keys.read_ai_key", lambda name: "sk-secret"
        )
        provider = DeepSeekProvider()
        monkeypatch.setattr(provider, "is_available", lambda: True)
        runner = _Runner({})

        provider.check_readiness(runner)

        assert runner.calls == []


def test_deepseek_reuses_the_claude_session_log_contract():
    """ai_system stays claude-code because the session writes Claude's JSONL.

    Log discovery, console-tag detection and the coding-done completion marker
    are all already correct for it; a separate ai_system would mean reproducing
    them for no gain.
    """
    assert DeepSeekProvider().executable == ClaudeCodeProvider().executable
    assert DeepSeekProvider().interactive is True


def test_an_anthropic_model_alias_is_not_silently_accepted_for_deepseek():
    """`--model opus` against DeepSeek is a configuration mistake, not a mapping."""
    assert "opus" not in DeepSeekProvider.MODEL_ALIASES
    assert "deepseek-v4-pro" in DeepSeekProvider.MODEL_ALIASES


def test_deepseek_does_not_inherit_the_fable_meter():
    """`deepseek + fable` is a typo, not a lane."""
    assert DeepSeekProvider.QUOTA_METERS == {}


# ---------------------------------------------------------------------------
# The policy actually USES the lane
#
# The circuit store has always kept distinct keys apart — that is what a primary
# key does. The regression these guard is the one that actually happened: a
# caller passing the bare provider name, so the lane row is never written and
# never read. They assert the key the policy chose, not the store's behaviour.
# ---------------------------------------------------------------------------


def _lane_config(tmp_path, *, provider: str, model: str):
    from issue_orchestrator.infra.config import AgentConfig, Config

    prompt = tmp_path / "prompt.md"
    prompt.write_text("Test prompt")
    config = Config(repo="test/repo", repo_root=tmp_path, max_concurrent_sessions=2)
    config.agents = {
        "agent:backend": AgentConfig(
            prompt_path=prompt, provider=provider, model=model
        ),
    }
    return config


def _policy(config, circuit, runner):
    from issue_orchestrator.control.provider_availability import (
        ProviderAvailabilityPolicy,
    )

    return ProviderAvailabilityPolicy(
        config=config,
        provider_resilience=circuit,
        readiness_probe=_installed_probe(runner),
    )


class TestThePolicyChoosesTheLaneNotTheProvider:
    def test_a_fable_agent_resolves_to_the_fable_lane(self, tmp_path, circuit):
        config = _lane_config(tmp_path, provider="claude-code", model="fable")
        policy = _policy(config, circuit, _Runner({"claude": SUBSCRIPTION_CLAUDE}))

        assert policy.lane_key_for_agent_label("agent:backend") == "claude-code:fable"
        assert policy.provider_for_agent_label("agent:backend") == "claude-code"

    def test_an_opus_agent_keeps_the_bare_provider_lane(self, tmp_path, circuit):
        config = _lane_config(tmp_path, provider="claude-code", model="opus")
        policy = _policy(config, circuit, _Runner({"claude": SUBSCRIPTION_CLAUDE}))

        assert policy.lane_key_for_agent_label("agent:backend") == "claude-code"

    def test_assess_launch_reports_the_lane_it_gated_on(self, tmp_path, circuit):
        config = _lane_config(
            tmp_path, provider="codex", model="gpt-5.3-codex-spark"
        )
        policy = _policy(config, circuit, _Runner({"codex": CHATGPT_CODEX}))

        outcome = policy.assess_launch("codex", model="gpt-5.3-codex-spark")

        assert outcome.provider == "codex"
        assert outcome.lane_key == "codex:spark"

    def test_an_exhausted_sub_meter_does_not_block_the_main_lane_agent(
        self, tmp_path, circuit
    ):
        """End to end: the spark meter is dry, astra work still launches."""
        now = datetime.now(timezone.utc)
        for _ in range(10):
            circuit.record_quota_failure(
                "codex:spark", error_summary="spark weekly limit", now=now
            )
        config = _lane_config(tmp_path, provider="codex", model="gpt-6-astra")
        policy = _policy(config, circuit, _Runner({"codex": CHATGPT_CODEX}))

        blocked = policy.assess_launch(
            "codex", model="gpt-5.3-codex-spark", now=now
        )
        usable = policy.assess_launch("codex", model="gpt-6-astra", now=now)

        assert blocked.circuit_open is True
        assert usable.circuit_open is False
        assert usable.may_launch is True

    def test_the_per_tick_sample_is_keyed_by_lane(self, tmp_path, circuit):
        """Two agents, one provider, two meters — two sampled outcomes."""
        from issue_orchestrator.control.provider_launch_readiness import (
            ProviderLaunchReadinessSampler,
        )
        from issue_orchestrator.infra.config import AgentConfig

        config = _lane_config(tmp_path, provider="codex", model="gpt-6-astra")
        config.agents["agent:fast"] = AgentConfig(
            prompt_path=tmp_path / "prompt.md",
            provider="codex",
            model="gpt-5.3-codex-spark",
        )
        policy = _policy(config, circuit, _Runner({"codex": CHATGPT_CODEX}))

        sample = ProviderLaunchReadinessSampler(config=config, policy=policy).sample()

        assert set(sample.outcomes) == {"codex", "codex:spark"}


def test_every_launch_path_passes_provider_credentials():
    """A new launch path must not silently omit the credential argument.

    The launcher funnels its four launch paths through one ``_spawn`` seam and
    the rework path owns its own, so there are two places a session is actually
    created. A provider whose key is dropped at either fails at run time with an
    authentication error that looks like a bad key rather than a missing
    wire-up, so this is checked structurally rather than by trusting call sites
    to stay in sync as paths are added.
    """
    import ast
    from pathlib import Path

    control = Path(__file__).resolve().parents[2] / "src/issue_orchestrator/control"
    offenders: list[str] = []

    for source in sorted(control.glob("session_*launcher*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name != "create_session" and not name.endswith("_create_session"):
                continue
            passes_secret = len(node.args) >= 5 or any(
                kw.arg == "secret_env" for kw in node.keywords
            )
            if not passes_secret:
                offenders.append(f"{source.name}:{node.lineno}")

    assert offenders == [], (
        "these session launches drop provider credentials, so an agent on a "
        f"key-authenticated provider would launch unauthenticated: {offenders}"
    )
