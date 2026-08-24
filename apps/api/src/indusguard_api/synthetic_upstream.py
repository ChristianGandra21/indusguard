"""API ASGI mínima e segura usada pelo conector público ``synthetic``.

Ela faz parte do produto, não do corpus de avaliação. O objetivo é exercitar HTTP, MCP e policy
sem depender de rede ou carregar a fixture industrial no deployment.
"""

from fastapi import FastAPI


def create_synthetic_upstream() -> FastAPI:
    """Cria dados demonstrativos determinísticos; PATCH nunca é chamado em modo simulate."""

    application = FastAPI(
        title="IndusGuard synthetic upstream",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.read_count = 0
    application.state.write_count = 0

    @application.get("/widgets/{widget_id}")
    async def get_widget(widget_id: str) -> dict[str, str]:
        application.state.read_count += 1
        return {"id": widget_id, "status": "active"}

    @application.patch("/widgets/{widget_id}")
    async def update_widget(widget_id: str) -> dict[str, str]:
        application.state.write_count += 1
        # Se esta função for alcançada, um teste de isolamento deve falhar: escritas públicas
        # continuam exclusivamente simuladas pelo executor antes do transporte ASGI.
        raise RuntimeError(f"escrita synthetic inesperada para {widget_id}")

    return application
