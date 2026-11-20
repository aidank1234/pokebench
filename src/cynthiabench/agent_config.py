"""Native execution profiles shared by Harbor and provenance collection."""
from pathlib import Path
from .native_agent import MODELS, CLAUDE_AUXILIARY_MODELS
from .native_runtime import cli_identity, calculation_python_identity
from .native_settings import calculation_python, proxy_admin_config

ROOT = Path(__file__).resolve().parents[2]
QUOTA_HALT_PATH = ROOT / 'work/provider-halt.json'
AGY_MODELS = {
    'neon': {'provider': 'agy', 'model': 'gemini-4-neon-xhigh', 'effort': 'xhigh'},
    'carbon': {'provider': 'agy', 'model': 'gemini-4-carbon-high', 'effort': 'high'},
}
OPENCODE_MODELS = {'flash': {'provider': 'opencode', 'model': 'deepseek-flash', 'effort': 'max'}}
ALL_MODELS = {**MODELS, **AGY_MODELS, **OPENCODE_MODELS}


def effective_agent_configs(models=None):
    """Pin only requested providers; unrelated CLIs need not be installed."""
    requested = list(MODELS) if models is None else list(models)
    selected = []
    for name in requested:
        matches = [spec for alias, spec in ALL_MODELS.items() if name in (alias, spec['model'])]
        if len(matches) != 1:
            raise ValueError('Unknown native model: ' + name)
        if matches[0] not in selected:
            selected.append(matches[0])
    pins = {p: cli_identity(p) for p in {s['provider'] for s in selected}}
    configs = {}
    for spec in selected:
        provider, model = spec['provider'], spec['model']
        common = {'model_name': model, 'skip_model_parsing': True}
        if provider in ('codex', 'claude'):
            configs[model] = {**common,
                'import_path': 'cynthiabench.native_agent:NativeSubscriptionAgent',
                'kwargs': {'effort': 'max', 'cli_pin': pins[provider],
                          'allowed_auxiliary_models': list(CLAUDE_AUXILIARY_MODELS) if provider == 'claude' else []},
                'context_policy': 'provider-native-cli-managed-history'}
        elif provider == 'agy':
            configs[model] = {**common,
                'import_path': 'cynthiabench.antigravity_agent:AntigravityAgent',
                'kwargs': {'effort': spec['effort'], 'cli_pin': pins[provider],
                          'quota_halt_path': str(QUOTA_HALT_PATH)},
                'context_policy': 'fresh-trial-local-client-state-no-resume-no-cross-trial-access',
                'calculation_python': calculation_python_identity(str(calculation_python()))}
        else:
            configs[model] = {**common,
                'import_path': 'cynthiabench.opencode_agent:OpenCodeFlashAgent',
                'kwargs': {'effort': 'max', 'cli_pin': pins[provider], 'max_output_tokens': 393216,
                          'proxy_admin_config_path': str(proxy_admin_config()),
                          'quota_halt_path': str(QUOTA_HALT_PATH)},
                'context_policy': 'fresh-trial-local-XDG-native-OpenCode-history-no-resume-no-cross-trial-access',
                'calculation_python': calculation_python_identity(str(calculation_python())),
                'advertised_model_version': 'DeepSeek-V4.1-Flash-1017',
                'authentication': 'Direct DeepSeek API via host-owned metered transport'}
    return configs


def harbor_agent_args(config):
    return {key: config[key] for key in ('import_path', 'model_name', 'skip_model_parsing', 'kwargs')}
