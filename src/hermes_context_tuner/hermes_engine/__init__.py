"""Hermes context-engine shim for hermes-context-tuner.

This file is copied into Hermes' `plugins/context_engine/context-tuner/` by:

    hermes-context-tuner install-hermes --set-config

It imports the package implementation when available. Keeping the shim tiny
makes it safe to re-copy after a Hermes update.
"""

from hermes_context_tuner.engine import ContextTunerEngine, context_tuner_command


def register(ctx):
    ctx.register_context_engine(ContextTunerEngine())
    if hasattr(ctx, "register_command"):
        ctx.register_command(
            "context-tuner",
            context_tuner_command,
            description="Show Hermes Context Tuner recovery/audit status.",
        )
