# Backend FastAPI

Este pacote contém as primeiras camadas executáveis do IndusGuard: configuração, modelos
Pydantic, validação de conectores, endpoints de inspeção e o executor HTTP protegido.

## O que ele faz hoje

1. encontra `connectors/*/profile.yaml`;
2. valida profile, OpenAPI e campos de domínio;
3. combina operações e políticas em um catálogo;
4. falha no startup se qualquer conector estiver inconsistente;
5. expõe liveness, readiness, versão, conectores e operações;
6. executa internamente GET com path, query, headers e body validados;
7. resolve `$ref` local e autenticação `none`, `context_header`, API key ou Bearer;
8. repete falhas transitórias somente quando a operação é idempotente;
9. simula escrita por default e mantém escrita real bloqueada sem policy engine;
10. redige campos sensíveis e normaliza execução, simulação, bloqueio e falha.

O executor ainda não possui rota pública. Os testes exercitam uma API simulada em memória. A
aplicação não usa LLM.

## Arquivos

| Arquivo | Responsabilidade |
|---|---|
| `settings.py` | Lê variáveis `INDUSGUARD_*` com defaults seguros. |
| `schemas.py` | Define profiles e respostas usando Pydantic. |
| `connectors.py` | Descobre, valida e consolida conectores. |
| `executor.py` | Valida, autentica, simula ou executa chamadas HTTP protegidas. |
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

## Testar somente o executor

```bash
.venv/bin/pytest apps/api/tests/test_executor.py -q
```

Os testes injetam `httpx.MockTransport`. Isso permite conferir método, URL, percent-encoding,
timeout e envelopes sem abrir porta ou acessar a internet.

## Limite atual do executor

O fluxo implementado aceita:

- operação habilitada;
- GET executado contra o upstream;
- POST/PATCH e demais escritas simulados quando o modo é `simulate`;
- autenticação `none`, `context_header`, API key em header/query e Bearer;
- parâmetros de path, query e header;
- `$ref` local em parâmetros e schemas;
- body JSON validado;
- retry de timeout, conexão, 429 e 5xx somente quando `idempotent=true`;
- redaction recursiva de campos do profile e de credenciais refletidas;
- resposta JSON ou vazia, com `attempts` e prévia tipada da simulação.

O modo `execute` ainda bloqueia qualquer escrita com `WRITE_POLICY_REQUIRED`. O próximo incremento
é a policy engine; nenhuma rota de execução será criada antes dessa fronteira determinística.
