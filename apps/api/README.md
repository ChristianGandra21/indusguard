# Backend FastAPI

Este pacote contém a primeira camada executável do IndusGuard: configuração, modelos Pydantic,
validação de conectores e endpoints de inspeção.

## O que ele faz hoje

1. encontra `connectors/*/profile.yaml`;
2. valida profile, OpenAPI e campos de domínio;
3. combina operações e políticas em um catálogo;
4. falha no startup se qualquer conector estiver inconsistente;
5. expõe liveness, readiness, versão, conectores e operações.

Ele ainda não chama APIs externas e não usa LLM.

## Arquivos

| Arquivo | Responsabilidade |
|---|---|
| `settings.py` | Lê variáveis `INDUSGUARD_*` com defaults seguros. |
| `schemas.py` | Define profiles e respostas usando Pydantic. |
| `connectors.py` | Descobre, valida e consolida conectores. |
| `main.py` | Cria a aplicação FastAPI e suas rotas. |
| `tests/` | Protege os contratos e as decisões de segurança. |

O [guia de leitura](../../docs/code-guide.md) explica o caminho do código em mais detalhes.

## Executar pela raiz do monorepo

```bash
make setup
make validate
make test
make dev-api
```

Não é necessário entrar em `apps/api`. Os comandos da raiz mantêm caminhos e configurações
consistentes.

## Executar manualmente

Depois de `make setup`:

```bash
.venv/bin/uvicorn indusguard_api.main:app \
  --app-dir apps/api/src \
  --reload
```

## Rotas

| Método e path | Significado |
|---|---|
| `GET /api/v1/health` | Processo HTTP está vivo. |
| `GET /api/v1/ready` | Startup terminou e catálogo foi carregado. |
| `GET /api/v1/version` | Versão, ambiente e modo de execução. |
| `GET /api/v1/connectors` | Resumo das integrações. |
| `GET /api/v1/connectors/{id}/operations` | Operações e políticas consolidadas. |

Swagger UI: `http://127.0.0.1:8000/docs`.

## Próxima responsabilidade

O próximo módulo será o executor HTTP genérico. Ele receberá somente operações já validadas pelo
catálogo e adicionará validação de argumentos, autenticação, allowlist, timeout, retry, redaction e
simulação de escritas.
