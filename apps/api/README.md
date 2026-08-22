# Backend FastAPI

Este pacote contém as primeiras camadas executáveis do IndusGuard: configuração, modelos
Pydantic, validação de conectores, endpoints de inspeção, policy engine e executor HTTP protegido.

## O que ele faz hoje

1. encontra `connectors/*/profile.yaml`;
2. valida profile, OpenAPI e campos de domínio;
3. combina operações e políticas em um catálogo;
4. falha no startup se qualquer conector estiver inconsistente;
5. expõe liveness, readiness, versão, conectores e operações;
6. executa internamente GET com path, query, headers e body validados;
7. resolve `$ref` local e autenticação `none`, `context_header`, API key ou Bearer;
8. repete falhas transitórias somente quando a operação é idempotente;
9. avalia identidade, escopos, permissão, pedido direto, justificativa e confirmação;
10. encaminha somente leituras permitidas e escritas simuladas ao executor;
11. mantém escrita real bloqueada mesmo depois de confirmação válida;
12. transforma operações habilitadas em tools MCP tipadas;
13. injeta contexto confiável fora dos argumentos controlados pelo modelo;
14. redige campos sensíveis e normaliza execução, simulação, bloqueio e falha.

MCP e executor não possuem rota pública. Os testes conectam um cliente MCP real ao servidor em
memória e simulam somente a API externa. A aplicação não usa LLM.

## Arquivos

| Arquivo | Responsabilidade |
|---|---|
| `settings.py` | Lê variáveis `INDUSGUARD_*` com defaults seguros. |
| `schemas.py` | Define profiles e respostas usando Pydantic. |
| `connectors.py` | Descobre, valida e consolida conectores. |
| `executor.py` | Valida, autentica, simula ou executa chamadas HTTP protegidas. |
| `policy.py` | Decide deterministicamente se uma proposta pode avançar. |
| `mcp_server.py` | Gera tools OpenAPI e encaminha chamadas ao `GuardedExecutor`. |
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

Para testar somente a fronteira política:

```bash
.venv/bin/pytest apps/api/tests/test_policy.py -q
```

Para testar descoberta e chamadas MCP em memória:

```bash
.venv/bin/pytest apps/api/tests/test_mcp_server.py -q
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

O `HttpExecutor` isolado continua devolvendo `WRITE_POLICY_REQUIRED` no modo `execute`. No caminho
correto, `PolicyEngine` e `HttpExecutor` são combinados pelo `GuardedExecutor` com o mesmo modo:

```text
PolicyEvaluationRequest
    -> PolicyEngine
        -> allow | simulate | require_confirmation | block
            -> HttpExecutor somente em allow/simulate
```

Permissões e escopos são claims confiáveis do runtime. Em uma futura integração com agente, o LLM
poderá propor argumentos, mas não fabricar `principal`, `resource_scopes` ou confirmação. Escritas
reais ainda terminam em `REAL_WRITE_DISABLED`, e nenhuma rota pública foi adicionada.

## Servidor MCP interno

`create_mcp_server()` recebe catálogo, `GuardedExecutor` e um `TrustedPolicyContextProvider`.
Cada operação habilitada vira `connector_id.operationId`; operações desabilitadas não aparecem.
O schema é derivado do OpenAPI, separa `path`, `query`, `headers` e `body`, fecha propriedades
inesperadas e copia referências locais para `$defs`.

O provider é obrigatório como argumento da factory e pode estar indisponível em runtime, caso em
que a chamada termina com `TRUSTED_CONTEXT_UNAVAILABLE`. Não há provider permissivo default. Tool
desconhecida, argumentos inválidos e falha interna também são erros MCP redigidos. `allow`,
`simulate`, `block`, `require_confirmation` e falhas HTTP continuam resultados estruturados
normais do fluxo protegido.
