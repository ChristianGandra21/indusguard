# Backend FastAPI

Este pacote contém as primeiras camadas executáveis do IndusGuard: configuração, modelos
Pydantic, conectores, executor protegido, policy engine, MCP, runtime LangGraph, persistência e
observabilidade internas.

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
15. valida integralmente o `domain.yaml` e suas referências a operações;
16. executa um StateGraph stateless de classificação, planejamento, tools e finalização;
17. converte aliases do modelo para tools MCP do conector selecionado;
18. limita modelo, tools, tempo e tamanho de evidências;
19. usa fake determinístico no CI e oferece Groq Free somente quando configurada.
20. persiste runs redigidas de forma transacional em SQLite ou PostgreSQL;
21. correlaciona modelo, tools, policy e HTTP por `run_id` em spans OpenTelemetry;
22. exporta JSONL local e OTLP opcional;
23. entrega a resposta com `OBSERVABILITY_DEGRADED` quando a auditoria falha.
24. consulta avaliações e traces por uma projeção pública que não carrega conteúdo livre;
25. expõe essas projeções em duas rotas GET para o dashboard estático.
26. autentica o proprietário com Bearer comparado em tempo constante;
27. limita o playground a três runs por hora e duas runs simultâneas;
28. sobrescreve identidade e injeta permissões fora do request controlado pelo cliente;
29. executa somente o conector `synthetic` por um upstream ASGI interno;
30. publica respostas redigidas sem token, confirmação ou digest da ação.

O MCP continua interno e não abre porta. A única rota do agente é o `POST /runs` protegido do
proprietário; ela aceita apenas `synthetic` e continua simulando toda escrita. Os testes usam
modelo fake e cliente MCP real em memória. A composição real só cria o adapter Groq quando
`GROQ_API_KEY` está configurada.

## Arquivos

| Arquivo | Responsabilidade |
|---|---|
| `settings.py` | Lê variáveis `INDUSGUARD_*` com defaults seguros. |
| `schemas.py` | Define profiles e respostas usando Pydantic. |
| `connectors.py` | Descobre, valida e consolida conectores. |
| `executor.py` | Valida, autentica, simula ou executa chamadas HTTP protegidas. |
| `policy.py` | Decide deterministicamente se uma proposta pode avançar. |
| `mcp_server.py` | Gera tools OpenAPI e encaminha chamadas ao `GuardedExecutor`. |
| `agent.py` | Define contratos, limites, modelo fake e o StateGraph interno. |
| `groq_gateway.py` | Implementa classificação/finalização estruturadas e tool calling na Groq. |
| `persistence.py` | Grava e reconstrói runs redigidas usando SQLAlchemy assíncrono. |
| `observability.py` | Configura spans, JSONL local, saúde dos exporters e OTLP opcional. |
| `runtime_factory.py` | Monta todas as camadas com banco e telemetria coerentes. |
| `dashboard.py` | Consulta somente colunas permitidas e monta projeções públicas. |
| `public_runs.py` | Esconde auth, quota, contexto confiável, runtime e projeção pública. |
| `synthetic_upstream.py` | Exercita GET em ASGI sem fixture industrial ou rede externa. |
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
| `GET /api/v1/ready` | Catálogo, migração, banco e host habilitado estão prontos. |
| `GET /api/v1/version` | Versão, ambiente e modo de execução. |
| `GET /api/v1/connectors` | Resumo das integrações. |
| `GET /api/v1/connectors/{id}/operations` | Operações e políticas consolidadas. |
| `GET /api/v1/evaluations/latest` | Resumo e runs da avaliação mais recente. |
| `GET /api/v1/runs/{run_id}/trace` | Timeline operacional sem conteúdo livre. |
| `GET /api/v1/playground/config` | Limites e campos públicos, sem segredos. |
| `POST /api/v1/runs` | Run stateless autenticada, apenas no conector synthetic. |

Swagger UI: `http://127.0.0.1:8000/docs`.

As rotas de dashboard retornam `404` quando não há registro e `503/DATASTORE_UNAVAILABLE` quando
o banco falha. O trace omite mensagem, resposta, argumentos, resultados, incertezas e digest. A
consulta usa `load_only`, portanto esses campos não são carregados para depois serem redigidos.
CORS aceita somente origens configuradas em `INDUSGUARD_CORS_ALLOWED_ORIGINS`; isso habilita o
navegador, mas não é tratado como autenticação.

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

Para testar o runtime completo sem internet:

```bash
.venv/bin/pytest apps/api/tests/test_agent_runtime.py -q
```

Para testar banco e traces:

```bash
.venv/bin/pytest apps/api/tests/test_persistence.py \
  apps/api/tests/test_observability.py -q
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

Permissões e escopos são claims confiáveis do runtime. O LLM propõe argumentos, mas não pode
fabricar `principal`, `resource_scopes` ou confirmação. Escritas reais ainda terminam em
`REAL_WRITE_DISABLED`; no playground, o processo roda em `simulate` e nenhuma escrita chega ao
transporte ASGI.

## Host público protegido

`PublicRunHost.execute()` é a interface única da rota de execução. A camada FastAPI não conhece
Groq, MCP, policy ou permissões: ela apenas entrega o Bearer e o `PublicRunRequest` ao host. O host
segue esta ordem para evitar consumo indevido de recursos:

```text
enabled -> autenticação -> conector/contexto -> concorrência -> quota -> AgentRuntime -> projeção
```

O request aceita somente `connector_id`, mensagem de até 2.000 caracteres, seed, campos de
contexto do `domain.yaml` e `direct_request`. Mesmo se o navegador enviar `user_id`, o backend o
substitui pelo principal fixo do proprietário. `principal`, permissões, escopos, confirmação e
digest são propriedades proibidas. Respostas usam `Cache-Control: no-store`.

A quota é persistida na tabela `public_run_quota`; reiniciar o processo não zera a janela de uma
hora. O limite de duas runs simultâneas é local ao processo e uma recusa por concorrência não
consome quota. O upstream `synthetic` executa GET em memória com `ASGITransport`; PATCH é
interrompido pela simulação antes desse transporte.

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

## Agente LangGraph e Groq Free

`AgentRuntime.run()` recebe `AgentRunRequest` e `TrustedRunContext` separadamente. Durante uma run,
ele cria um provider vinculado ao contexto, abre um cliente MCP em memória e executa os nós
`validate -> classify -> plan -> tools -> finalize`. Somente as tools habilitadas do conector
selecionado chegam ao planejador, com aliases como `tractian__getAsset`.

O classificador aceita apenas IDs do domínio. O planejador chama tools sequencialmente. O
finalizador é uma chamada separada, sem tools, e só pode citar IDs de evidência realmente
coletados. Resultados externos continuam em `ToolMessage`; instruções contidas neles não recebem
autoridade de system prompt.

Defaults defensivos:

- `openai/gpt-oss-20b`, temperatura zero e seed da run;
- 8 chamadas de modelo e 12 tools;
- 60 segundos;
- 32 KiB por evidência e 128 KiB por run;
- `parallel_tool_calls=false`;
- sem fallback pago ou Ollama.

Sem `GROQ_API_KEY`, o adapter real falha na construção, mas toda a suíte offline funciona. Com a
chave, o smoke manual é executado por:

```bash
# Defina GROQ_API_KEY no .env ignorado pelo Git e execute:
.venv/bin/pytest apps/api/tests/test_agent_runtime.py -m live -q
```

## Persistência e traces

Execute `make migrate` antes de usar o recorder local. O default cria `.data/indusguard.db`; o
arquivo e `.data/traces.jsonl` são ignorados pelo Git. `AgentRuntime` aceita `recorder` e
`telemetry` por injeção, enquanto `create_internal_agent_host()` é a composition root recomendada
para scripts e futuras avaliações.

Uma run é salva atomicamente com tools, evidências e decisões políticas. O recorder nunca recebe
`TrustedRunContext`, portanto não persiste principal, permissões, escopos ou confirmação. Prompts,
headers, URLs e respostas ilimitadas também ficam fora do banco e dos spans.

Se a persistência ou o JSONL falhar, a decisão funcional não muda. O resultado inclui:

```json
{
  "observability": {
    "status": "degraded",
    "warning_code": "OBSERVABILITY_DEGRADED",
    "persistence": "failed",
    "local_trace": "recorded"
  }
}
```
