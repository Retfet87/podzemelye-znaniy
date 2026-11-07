"""Роутер: разбирает событие и отдаёт его первому подходящему обработчику."""
from app.handlers.context import Context


class Router:
    def __init__(self, deps, handlers):
        self.deps = deps
        self.handlers = list(handlers)

    def dispatch(self, update):
        ctx = Context.parse(self.deps, update)
        if ctx.kind == "ignored":
            return
        method = "on_message" if ctx.kind == "message" else "on_callback"
        for handler in self.handlers:
            if getattr(handler, method)(ctx):
                self._keep_single_draft(handler, ctx.actor)
                return

    def _keep_single_draft(self, owner, actor):
        """У человека может быть открыт только один черновик: новый диалог закрывает чужой."""
        if owner.drafts is None or actor not in owner.drafts:
            return
        for other in self.handlers:
            if other is not owner and other.drafts is not None:
                other.drafts.pop(actor, None)
