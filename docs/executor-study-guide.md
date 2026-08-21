# Do HTTP ao executor do IndusGuard

> Aula progressiva, do zero ao código atual.
>
> Última atualização: 20 de agosto de 2026.

Este guia foi escrito para quem abriu
[`executor.py`](../apps/api/src/indusguard_api/executor.py) e encontrou muitos conceitos ao mesmo
tempo: HTTP, OpenAPI, JSON Schema, Pydantic, código assíncrono, segurança, catálogo e testes.

Não comece tentando memorizar cada função. Primeiro entenda o problema; depois acompanhe uma única
operação, `getAsset`, atravessando todo o sistema.

## Como usar esta aula

Siga esta ordem:

1. aprenda o mínimo de HTTP;
2. entenda de onde vêm os arquivos Tractian;
3. diferencie OpenAPI, profile e domain;
4. entenda como o catálogo é criado no startup;
5. acompanhe `getAsset` do pedido interno até a resposta;
6. volte às funções auxiliares do executor;
7. execute o laboratório e os testes;
8. confira o que ainda não foi implementado.

Se você já conhece HTTP e Python, pode começar em [A arquitetura atual](#parte-iii--a-arquitetura-atual).

---

# Parte I — Fundamentos

## 1. O problema que o projeto quer resolver

Imagine que, no futuro, uma pessoa escreva para um agente:

> “Consulte o ativo `asset_M101` e veja se existe alguma informação relevante.”

Para responder, o agente pode precisar chamar uma API industrial. Mas não é seguro deixar um LLM
escolher livremente:

- qualquer URL;
- qualquer endpoint;
- qualquer identidade de usuário;
- qualquer corpo JSON;
- qualquer operação de escrita.

O LLM é probabilístico. Já regras como “esta URL é permitida?” ou “este parâmetro é obrigatório?”
devem ser determinísticas: a mesma entrada precisa produzir a mesma decisão.

O IndusGuard coloca uma camada determinística entre o futuro agente e a API externa:

```mermaid
flowchart LR
    P[Pessoa] -. futuro .-> A[Agente/LLM]
    A -. pedido estruturado .-> I[IndusGuard]
    I --> C[Catálogo validado]
    I --> E[Executor HTTP protegido]
    E --> T[API externa]
```

Hoje, o catálogo, o executor GET e a simulação de escritas existem. O agente, MCP, policy engine e
frontend ainda não existem.

## 2. O que é uma API?

Uma API é uma interface usada por dois programas para se comunicar.

Em vez de uma pessoa abrir uma tela e clicar, um programa envia um request, por exemplo:

```http
GET /assets/asset_M101?seed=case-01 HTTP/1.1
Host: localhost:8000
x-user-id: usr_001
```

O servidor pode responder:

```http
HTTP/1.1 200 OK
Content-Type: application/json

{
  "status": "complete",
  "data": {
    "id": "asset_M101",
    "name": "Motor principal da forja"
  }
}
```

Há duas direções:

```text
cliente  -- request -->  servidor
cliente  <-- response -- servidor
```

Neste projeto:

- `HttpExecutor` é o cliente;
- a API industrial é o servidor externo, também chamado de **upstream**;
- `httpx` é a biblioteca Python que envia o request.

## 3. Anatomia de um request HTTP

Considere esta URL:

```text
http://localhost:8000/assets/asset_M101?seed=case-01
```

| Parte | Valor | Significado |
|---|---|---|
| esquema | `http` | Protocolo usado |
| host | `localhost` | Máquina de destino |
| porta | `8000` | Porta do serviço |
| path | `/assets/asset_M101` | Recurso solicitado |
| query | `seed=case-01` | Opção adicional da consulta |

Um request possui cinco partes que importam para o executor:

### Método

Indica a ação desejada:

| Método | Uso comum | Tratamento atual |
|---|---|---|
| `GET` | consultar | pode chegar à rede |
| `POST` | criar ou iniciar ação | simulado; execução real bloqueada |
| `PUT` | substituir | simulado; execução real bloqueada |
| `PATCH` | alterar parcialmente | simulado; execução real bloqueada |
| `DELETE` | remover | simulado; execução real bloqueada |

O método não é escolhido pelo request interno. Ele vem do OpenAPI da operação.

### Path

Identifica o recurso:

```text
/assets/{assetId}
```

`{assetId}` é um espaço reservado. Com `assetId="asset_M101"`, o path final será:

```text
/assets/asset_M101
```

### Query

São os pares depois de `?`:

```text
?seed=case-01&status=current
```

### Headers

São metadados do request:

```text
x-user-id: usr_001
content-type: application/json
```

No conector Tractian, `x-user-id` representa a identidade da pessoa.

### Body

É o conteúdo enviado, normalmente em operações de escrita:

```json
{
  "justification": "mudança aprovada para manutenção preventiva",
  "changes": {
    "criticality": "high"
  }
}
```

`GET getAsset` não declara body. `PATCH updateAssetConfig` declara.

## 4. Anatomia de uma response HTTP

A resposta possui:

- status HTTP, como `200`, `404`, `429` ou `503`;
- headers;
- body, que neste runtime precisa ser JSON ou vazio.

Famílias de status:

| Faixa | Interpretação geral |
|---|---|
| `2xx` | sucesso HTTP |
| `4xx` | problema no request ou na autorização |
| `5xx` | problema no servidor upstream |

O executor não devolve diretamente o formato específico de cada API. Ele cria um envelope comum
com `outcome`, `status_code`, `data`, `error` e `latency_ms`.

## 5. JSON, YAML, schema e validação

### JSON

Formato de dados usado no request e na response:

```json
{
  "id": "asset_M101",
  "criticality": "high"
}
```

### YAML

Formato de configuração legível por pessoas:

```yaml
id: asset_M101
criticality: high
```

O projeto usa YAML para OpenAPI, profile e domain.

### Schema

Um schema descreve as regras de um valor. Exemplo:

```yaml
schema:
  type: string
  enum: [low, medium, high, critical]
```

O valor `high` é válido. O valor `impossible` não é.

### Validação

Validar significa comparar um valor com regras conhecidas antes de usá-lo.

```text
valor + schema -> válido ou inválido
```

No executor, a biblioteca `jsonschema` valida argumentos contra os schemas do OpenAPI.

## 6. O que é OpenAPI?

OpenAPI é um contrato que descreve uma API de forma legível por pessoas e máquinas.

Ele responde perguntas técnicas:

- quais endpoints existem?
- qual método cada um usa?
- quais parâmetros são obrigatórios?
- qual body é aceito?
- quais respostas podem existir?

Ele **não** responde se o futuro agente deve ter permissão para usar uma operação. Essa decisão fica
no `profile.yaml` do IndusGuard.

A frase mais importante da arquitetura é:

> OpenAPI descreve o que a API consegue fazer; o profile descreve o que o IndusGuard decide
> permitir.

## 7. `operationId`: o nome estável da operação

No contrato aparece:

```yaml
operationId: getAsset
```

O `operationId` conecta várias camadas:

```mermaid
flowchart LR
    O[OpenAPI: getAsset] --> P[Profile: política de getAsset]
    P --> D[Domain: intenção usa getAsset]
    D --> R[Request: operation_id=getAsset]
    R --> E[Executor resolve getAsset]
```

O executor recebe `operation_id="getAsset"`, não `url="http://..."`. Assim, somente operações
conhecidas podem ser resolvidas.

---

# Parte II — De onde vem cada código

## 8. Material fornecido e código construído no projeto

É importante não misturar a origem dos artefatos.

| Artefato | Origem e papel |
|---|---|
| [`connectors/tractian/openapi.yaml`](../connectors/tractian/openapi.yaml) | Cópia versionada e normalizada do contrato fornecido no desafio Tractian × Inteli |
| API FastAPI, dados, cenários e testes da fixture | Fornecidos pelos stakeholders, mas o código-fonte da fixture não está neste workspace |
| [`connectors/tractian/profile.yaml`](../connectors/tractian/profile.yaml) | Política local criada para o IndusGuard |
| [`connectors/tractian/domain.yaml`](../connectors/tractian/domain.yaml) | Vocabulário e contexto organizados para o IndusGuard |
| [`connectors.py`](../apps/api/src/indusguard_api/connectors.py) | Loader e catálogo genérico do IndusGuard |
| [`schemas.py`](../apps/api/src/indusguard_api/schemas.py) | Contratos Pydantic do IndusGuard |
| [`executor.py`](../apps/api/src/indusguard_api/executor.py) | Executor HTTP protegido do IndusGuard |
| [`test_executor.py`](../apps/api/tests/test_executor.py) | Testes do executor do IndusGuard, não os testes oficiais da fixture |
| [`executor_walkthrough.py`](../apps/api/examples/executor_walkthrough.py) | Laboratório didático criado para acompanhar esta aula |

O relatório [`stakeholder-material.md`](stakeholder-material.md#path-duplicado-no-openapi) registra
que o OpenAPI original repetia `/assets/{assetId}`: uma vez para GET e outra para PATCH. Como YAML
não permite essa duplicidade de maneira segura, a cópia versionada reúne os dois métodos sob o mesmo
path. Segundo o relatório, nenhuma outra alteração foi feita no contrato.

Portanto, podemos estudar diretamente o contrato Tractian disponível no repositório, mas não criar
links para o código interno da API FastAPI recebida porque ele não está presente aqui.

## 9. Os três arquivos do conector Tractian

```text
connectors/tractian/
├── openapi.yaml   # capacidade técnica
├── profile.yaml   # política local
└── domain.yaml    # vocabulário e contexto
```

### 9.1 O OpenAPI fornecido: o que a API oferece

Este é um trecho real de
[`connectors/tractian/openapi.yaml`](../connectors/tractian/openapi.yaml#L331-L379):

```yaml
/assets/{assetId}:
  get:
    tags: [Ativos]
    operationId: getAsset
    parameters:
      - $ref: '#/components/parameters/Seed'
      - name: assetId
        in: path
        required: true
        schema: { type: string }

  patch:
    tags: [Ativos]
    operationId: updateAssetConfig
    security: [{ UserContext: [] }]
    parameters:
      - name: assetId
        in: path
        required: true
        schema: { type: string }
    requestBody:
      required: true
      content:
        application/json:
          schema:
            allOf:
              - $ref: '#/components/schemas/ActionRequest'
              - type: object
                properties:
                  changes:
                    type: object
```

O contrato afirma:

- o mesmo recurso possui uma consulta `GET` e uma alteração `PATCH`;
- `assetId` é obrigatório, fica no path e precisa ser string;
- `getAsset` aceita o parâmetro reutilizável `Seed`;
- `updateAssetConfig` exige body JSON;
- o body reutiliza schemas com `$ref` e `allOf`.

O parâmetro `Seed` está em
[`components.parameters`](../connectors/tractian/openapi.yaml#L49-L55):

```yaml
Seed:
  name: seed
  in: query
  required: false
  schema: { type: string }
```

`$ref: '#/components/parameters/Seed'` significa: “vá até esta posição dentro do mesmo documento e
reuse o objeto”. O prefixo `#/` indica uma referência local.

### 9.2 O profile do IndusGuard: o que decidimos permitir

Trecho real de
[`connectors/tractian/profile.yaml`](../connectors/tractian/profile.yaml#L1-L40):

```yaml
id: tractian
openapi: ./openapi.yaml
base_url_env: TRACTIAN_API_URL

allowed_base_urls:
  - http://localhost:8000

auth:
  type: context_header
  name: x-user-id
  context_field: user_id

operations:
  getCompany: &read_operation
    enabled: true
    access: read
    risk: low
    timeout_seconds: 10
    max_retries: 2
    idempotent: true

  getAsset: *read_operation
```

Interpretação:

- a URL efetiva precisa vir da variável `TRACTIAN_API_URL`;
- mesmo vinda do ambiente, ela só é aceita se estiver na allowlist;
- o header `x-user-id` será construído com o campo `user_id` do contexto;
- `getAsset` reutiliza a política `read_operation` por meio da âncora YAML;
- a operação está habilitada, é leitura, possui risco baixo e timeout de 10 segundos.

O executor aplica `max_retries: 2` somente porque essa política também declara `idempotent: true`.
Timeout, conexão, HTTP 429 e 5xx podem ser repetidos; erros 4xx comuns não são.

### 9.3 O domain do IndusGuard: como interpretar o domínio

Trecho real de
[`connectors/tractian/domain.yaml`](../connectors/tractian/domain.yaml#L1-L25):

```yaml
id: tractian
language: pt-BR

context_fields:
  - user_id
  - company_id
  - asset_id
  - case_id

terminology:
  asset: ativo industrial monitorado
  baseline: estado normal aprendido para um ativo ou ponto de medição

intents:
  - id: contextualizar
    evidence_operations: [getCurrentUser, getCompany, getAsset]
```

Hoje, a parte mais importante para o executor é `context_fields`: o loader exige que
`auth.context_field: user_id` aponte para um campo declarado no domínio.

Terminologia e intenções estão preparadas para o futuro agente e a futura interface, mas ainda não
orientam a execução HTTP.

---

# Parte III — A arquitetura atual

## 10. Visão geral do que existe

```mermaid
flowchart TD
    subgraph Arquivos do conector
        O[openapi.yaml]
        P[profile.yaml]
        D[domain.yaml]
    end

    O --> C[ConnectorCatalog]
    P --> C
    D --> C
    C --> V{Tudo é válido?}
    V -- não --> S[Startup falha]
    V -- sim --> M[Catálogo em memória]
    M --> F[Rotas FastAPI de inspeção]
    M --> E[HttpExecutor interno]
    E --> U[API upstream ou MockTransport]
```

Há dois momentos diferentes:

1. **startup:** arquivos são descobertos, validados e transformados em catálogo;
2. **execução:** um request interno escolhe uma operação desse catálogo e tenta executá-la.

## 11. O startup: de arquivos para objetos confiáveis

Em [`main.py`](../apps/api/src/indusguard_api/main.py#L25-L51), a aplicação cria o catálogo e o
carrega no lifespan:

```python
settings = Settings(connectors_dir=connectors_dir) if connectors_dir else Settings()
catalog = ConnectorCatalog(settings.connectors_dir)

@asynccontextmanager
async def lifespan(_: FastAPI):
    catalog.load()
    yield

application.state.connector_catalog = catalog
```

Em linguagem simples:

1. `Settings` descobre onde está a pasta `connectors/`;
2. `ConnectorCatalog` começa vazio;
3. `catalog.load()` lê todos os conectores;
4. se qualquer conector for inválido, o startup falha;
5. se todos forem válidos, o catálogo fica disponível em `app.state`.

Isso é **fail-fast**: é melhor não iniciar do que anunciar uma integração parcialmente válida.

## 12. Como o catálogo combina OpenAPI e profile

A função
[`_parse_operations()`](../apps/api/src/indusguard_api/connectors.py#L334-L389) percorre cada path e
método do OpenAPI:

```python
for path, path_item in spec.get("paths", {}).items():
    for method, raw_operation in path_item.items():
        operation_id = raw_operation.get("operationId")

        policy = policies.get(operation_id, OperationPolicy())
        summary = _build_operation(path, normalized_method, raw_operation, policy)

        operations.append(
            RuntimeOperation(
                summary=summary,
                parameters=_merge_parameters(spec, path_item, raw_operation),
                request_body=_request_body(spec, raw_operation),
                reference_document=spec,
            )
        )
```

O objeto final combina:

| Campo | Origem |
|---|---|
| método e path | OpenAPI |
| parâmetros e body | OpenAPI |
| `enabled`, risco e timeout | profile |
| documento para resolver `$ref` | OpenAPI completo |

Se uma operação existe no OpenAPI, mas não no profile, esta linha cria uma política default:

```python
policy = policies.get(operation_id, OperationPolicy())
```

Em [`OperationPolicy`](../apps/api/src/indusguard_api/schemas.py#L76-L102), `enabled` começa como
`False`. Portanto, endpoint novo não ganha autorização automaticamente.

## 13. Como `$ref` de parâmetro é resolvido

O `getAsset` referencia `Seed`. O catálogo resolve isso antes de entregar a operação ao executor.

Trecho de
[`_resolve_json_pointer()`](../apps/api/src/indusguard_api/connectors.py#L248-L270):

```python
if not reference.startswith("#/"):
    raise ConnectorValidationError(...)

current = document
for raw_token in reference[2:].split("/"):
    token = raw_token.replace("~1", "/").replace("~0", "~")
    current = current[token]

return deepcopy(current)
```

Para `#/components/parameters/Seed`, o percurso conceitual é:

```text
document
└── components
    └── parameters
        └── Seed
            ├── name: seed
            ├── in: query
            └── schema: {type: string}
```

A resolução usa somente o documento em memória. Referências para arquivo ou URL são recusadas.

## 14. A visão pública e a visão interna

O catálogo possui duas representações importantes:

- `ConnectorDetails`: resumo seguro para rotas públicas;
- `ResolvedOperation`: metadados internos necessários ao executor.

[`resolve_operation()`](../apps/api/src/indusguard_api/connectors.py#L498-L522) devolve:

```python
return ResolvedOperation(
    profile=deepcopy(connector.profile),
    operation=deepcopy(operation.summary),
    parameters=deepcopy(operation.parameters),
    request_body=deepcopy(operation.request_body),
    reference_document=deepcopy(operation.reference_document),
)
```

As cópias impedem que um consumidor altere o catálogo compartilhado por acidente.

## 15. O executor ainda não é uma rota

As rotas atuais aparecem em [`main.py`](../apps/api/src/indusguard_api/main.py#L53-L112):

- `/health`;
- `/ready`;
- `/version`;
- `/connectors`;
- `/connectors/{id}/operations`.

Não existe `/execute`. Hoje, `HttpExecutor` é chamado diretamente pelos testes e pelo laboratório
didático. Isso impede expor execução antes da futura policy engine.

---

# Parte IV — Python necessário para ler o executor

## 16. Funções e type hints

Veja a assinatura:

```python
def _serialize_primitive(value: Any, *, location: str) -> str:
```

Leitura:

- `def`: define uma função;
- `value: Any`: recebe um valor de tipo ainda desconhecido;
- `*`: argumentos seguintes precisam ser nomeados;
- `location: str`: recebe uma string;
- `-> str`: promete devolver uma string.

Type hints ajudam pessoas, IDEs e verificadores. Python não aplica todas essas anotações sozinho em
runtime; validação real é feita por Pydantic e JSON Schema onde necessário.

## 17. `Mapping`, `dict`, `tuple` e `Any`

- `Mapping[str, Any]`: estrutura somente de leitura com chaves string;
- `dict[str, str]`: dicionário mutável de strings;
- `tuple[tuple[str, str], ...]`: sequência imutável de pares, usada na query;
- `Any`: valor cujo tipo será conferido por outra camada.

A query é uma sequência de pares porque uma chave pode se repetir:

```python
(("labels", "critical"), ("labels", "monitored"))
```

Um `dict` comum não representaria duas ocorrências independentes da mesma chave.

## 18. Classes, `self` e injeção de dependência

Trecho de [`HttpExecutor.__init__()`](../apps/api/src/indusguard_api/executor.py#L435-L444):

```python
def __init__(
    self,
    catalog: ConnectorCatalog,
    *,
    environment: Mapping[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> None:
    self._catalog = catalog
    self._environment = os.environ if environment is None else environment
    self._client = client
```

`self` é a instância atual. O construtor guarda três dependências:

- catálogo;
- variáveis de ambiente;
- cliente HTTP opcional.

Em produção, pode usar `os.environ` e criar um cliente. Em testes, injetamos ambiente controlado e
cliente com transporte falso. Isso é **injeção de dependência**.

## 19. `async`, `await` e I/O

A rede envolve espera. Por isso:

```python
async def execute(...):
    response = await self._send_request(...)
```

`async def` cria uma corrotina. `await` suspende aquela corrotina enquanto o I/O não termina,
permitindo que o event loop faça outro trabalho.

As funções puramente locais, como `_render_path()`, são síncronas. Apenas a fronteira de transporte
precisa de `await`.

## 20. Exceções

Validações previsíveis usam:

```python
raise ExecutionValidationError("INVALID_BASE_URL", "mensagem segura")
```

`execute()` captura essa exceção:

```python
except ExecutionValidationError as exc:
    return self._blocked(request, started_at, exc.code, str(exc))
```

Exceção interna vira resultado de domínio. Isso evita espalhar stack traces e detalhes sensíveis.

## 21. Dataclass imutável e sentinela

[`PreparedRequest`](../apps/api/src/indusguard_api/executor.py#L46-L53) agrupa partes já validadas:

```python
@dataclass(frozen=True)
class PreparedRequest:
    path: str
    query: tuple[tuple[str, str], ...]
    headers: dict[str, str]
    body: Any
```

`frozen=True` impede reatribuir campos por acidente depois da preparação.

O arquivo também declara:

```python
NO_BODY: Final = object()
```

Essa sentinela distingue:

```python
ExecutionArguments()           # body ausente
ExecutionArguments(body=None)  # JSON null enviado explicitamente
```

`None` não pode representar os dois estados porque `null` é um valor JSON válido.

---

# Parte V — Entrada e saída do executor

## 22. Os modelos Pydantic

O request interno é definido em
[`schemas.py`](../apps/api/src/indusguard_api/schemas.py#L160-L188):

```python
class ExecutionArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: dict[str, Any] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, Any] = Field(default_factory=dict)
    body: Any = None


class OperationExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    operation_id: str = Field(min_length=1)
    arguments: ExecutionArguments = Field(default_factory=ExecutionArguments)
    context: dict[str, Any] = Field(default_factory=dict)
```

`extra="forbid"` rejeita campos desconhecidos. Um typo não é silenciosamente ignorado.

Separar `path`, `query`, `headers` e `body` evita ambiguidade. O mesmo nome pode existir em posições
diferentes, mas cada valor continua ligado ao local correto do request HTTP.

O resultado está em
[`OperationExecutionResult`](../apps/api/src/indusguard_api/schemas.py#L191-L208):

```python
class OperationExecutionResult(BaseModel):
    connector_id: str
    operation_id: str
    outcome: ExecutionOutcome
    status_code: Annotated[int, Field(ge=100, le=599)] | None = None
    data: Any = None
    error: ExecutionErrorDetails | None = None
    latency_ms: Annotated[float, Field(ge=0)]
```

## 23. O pedido interno de `getAsset`

```python
OperationExecutionRequest(
    connector_id="tractian",
    operation_id="getAsset",
    arguments=ExecutionArguments(
        path={"assetId": "asset_M101"},
        query={"seed": "case-01"},
    ),
    context={"user_id": "usr_001"},
)
```

Note o que **não** aparece:

- método HTTP;
- URL-base;
- path `/assets/{assetId}`;
- nome do header de autenticação;
- timeout.

Esses dados vêm de contratos validados, não de quem pede a execução.

## 24. O resultado comum

```python
OperationExecutionResult(
    connector_id="tractian",
    operation_id="getAsset",
    outcome=ExecutionOutcome.EXECUTED,
    status_code=200,
    data={"status": "complete", "data": {"id": "asset_M101"}},
    error=None,
    attempts=1,
    simulation=None,
    latency_ms=1.234,
)
```

| Outcome | Rede acessada? | Significado |
|---|---:|---|
| `blocked` | não | regra local impediu a chamada |
| `failed` | sim | transporte ou resposta apresentou problema |
| `executed` | sim | upstream respondeu 2xx com JSON válido ou body vazio |
| `simulated` | não | escrita validada transformada em prévia redigida |

---

# Parte VI — `HttpExecutor.execute()` por etapas

## 25. O mapa completo

```mermaid
flowchart TD
    A[OperationExecutionRequest] --> B{Conector existe?}
    B -- não --> B1[BLOCKED: CONNECTOR_NOT_FOUND]
    B -- sim --> C{Operação existe?}
    C -- não --> C1[BLOCKED: OPERATION_NOT_FOUND]
    C -- sim --> D{Está habilitada?}
    D -- não --> D1[BLOCKED: OPERATION_DISABLED]
    D -- sim --> E[Validar path, query, headers, auth e body]
    E --> W{É escrita?}
    W -- sim --> S{Modo simulate?}
    S -- sim --> S1[SIMULATED: prévia redigida]
    S -- não --> S2[BLOCKED: WRITE_POLICY_REQUIRED]
    W -- não --> F[Resolver URL do ambiente]
    F --> G{URL pertence à allowlist?}
    G -- não --> G1[BLOCKED]
    G -- sim --> I[Enviar GET com timeout e retry idempotente]
    I --> J{Erro de transporte?}
    J -- sim --> J1[FAILED]
    J -- não --> K{Body vazio ou JSON válido?}
    K -- não --> K1[FAILED: INVALID_JSON_RESPONSE]
    K -- sim --> L{Status 2xx?}
    L -- não --> L1[FAILED: UPSTREAM_HTTP_ERROR]
    L -- sim --> M[EXECUTED]
```

## 26. Etapa 1 — localizar conector e operação

Trecho de [`execute()`](../apps/api/src/indusguard_api/executor.py#L446-L473):

```python
connector = self._catalog.get(request.connector_id)
if connector is None:
    return self._blocked(..., "CONNECTOR_NOT_FOUND", ...)

resolved = self._catalog.resolve_operation(
    request.connector_id,
    request.operation_id,
)
if resolved is None:
    return self._blocked(..., "OPERATION_NOT_FOUND", ...)

if not resolved.operation.enabled:
    return self._blocked(..., "OPERATION_DISABLED", ...)
```

Há dois acessos porque o código quer distinguir:

- conector inexistente;
- conector existente, mas operação inexistente.

Operação desabilitada também para antes de preparar qualquer request.

## 27. Etapa 2 — preparar todas as partes

[`_prepare_request()`](../apps/api/src/indusguard_api/executor.py#L553-L570):

```python
path = _render_path(resolved, request.arguments.path)
auth = _build_auth_material(
    resolved,
    request.context,
    request.arguments.headers,
    request.arguments.query,
    self._environment,
    include_secrets=include_auth_secrets,
)
query = _build_query(resolved, request.arguments.query) + auth.query
headers = _build_headers(resolved, request.arguments.headers)
headers.update(auth.headers)
body = _build_body(resolved, request.arguments)

return PreparedRequest(..., sensitive_values=auth.sensitive_values)
```

Até aqui não há rede. É uma fase de compilação:

```text
argumentos não confiáveis + contrato validado
                    ↓
            PreparedRequest
```

## 28. Etapa 3 — resolver e aprovar a URL-base

[`_resolve_base_url()`](../apps/api/src/indusguard_api/executor.py#L572-L601):

```python
variable_name = resolved.profile.base_url_env
raw_base_url = self._environment.get(variable_name)
base_url = _normalize_base_url(raw_base_url)

allowed = {
    _normalize_base_url(value)
    for value in resolved.profile.allowed_base_urls
}

if base_url not in allowed:
    raise ExecutionValidationError("BASE_URL_NOT_ALLOWED", ...)
```

O ambiente escolhe qual destino usar, mas o profile limita os destinos possíveis.

Para Tractian:

```text
profile.base_url_env
        ↓
TRACTIAN_API_URL
        ↓ ambiente
http://localhost:8000
        ↓ comparação
profile.allowed_base_urls contém exatamente esse destino
```

## 29. Etapa 4 — simular escrita sem rede

Trecho de [`execute()`](../apps/api/src/indusguard_api/executor.py#L481-L489):

```python
if resolved.operation.access is AccessMode.WRITE:
    prepared = self._prepare_request(
        resolved,
        request,
        include_auth_secrets=False,
    )
    if self._execution_mode == "simulate":
        return self._simulated(request, resolved, prepared, started_at)
    return self._blocked(
        request,
        started_at,
        "WRITE_POLICY_REQUIRED",
        "a execução real de escrita exige uma decisão da policy engine",
    )
```

O body de um PATCH é validado antes do resultado. No modo default, o executor devolve uma
`SimulatedAction` com método, path relativo, query, nomes de headers e body redigido. A prévia não
resolve a URL-base, não lê API key/Bearer e não abre conexão.

`simulated` não significa sucesso externo. Ele significa apenas que a ação é tecnicamente válida e
poderia seguir para uma futura avaliação de política. No modo `execute`, ela continua bloqueada.

## 30. Etapa 5 — abrir a fronteira de rede

Somente depois das barreiras anteriores ocorre:

```python
response = await self._send_request(
    method=resolved.operation.method,
    url=url,
    query=prepared.query,
    headers=prepared.headers,
    body=prepared.body,
    timeout_seconds=resolved.operation.timeout_seconds,
)
```

[`_send_request()`](../apps/api/src/indusguard_api/executor.py#L603-L625) monta argumentos do HTTPX:

```python
request_arguments = {
    "params": query,
    "headers": headers,
    "timeout": timeout_seconds,
}

if body is not NO_BODY:
    request_arguments["json"] = body

return await self._client.request(method, url, **request_arguments)
```

Essa é a fronteira crítica: tudo antes valida e prepara; esta função pode acessar o upstream.

## 31. Etapa 6 — repetir somente falhas transitórias seguras

Trecho de [`execute()`](../apps/api/src/indusguard_api/executor.py#L501-L516):

```python
max_attempts = 1 + (
    resolved.operation.max_retries
    if resolved.operation.idempotent
    else 0
)

for attempts in range(1, max_attempts + 1):
    try:
        response = await self._send_request(...)
    except httpx.TimeoutException:
        if attempts < max_attempts:
            await self._wait_before_retry(attempts)
            continue
        return self._failed(..., retryable=True, attempts=attempts)
```

A mensagem original da exceção não é devolvida. Ela poderia conter host, porta ou outros detalhes
internos. Entre tentativas há backoff exponencial limitado; os testes injetam atraso zero para
continuarem rápidos. Operação não idempotente faz exatamente uma tentativa, mesmo que o profile
contenha `max_retries`.

## 32. Etapa 7 — interpretar JSON e status

Trecho de [`execute()`](../apps/api/src/indusguard_api/executor.py#L518-L550):

```python
try:
    data = response.json() if response.content else None
except ValueError:
    return self._failed(..., "INVALID_JSON_RESPONSE", ...)

data = _redact(data, redaction_fields, sensitive_values)

if not 200 <= response.status_code < 300:
    return OperationExecutionResult(
        outcome=ExecutionOutcome.FAILED,
        status_code=response.status_code,
        data=data,
        error=ExecutionErrorDetails(
            code="UPSTREAM_HTTP_ERROR",
            retryable=response.status_code == 429 or response.status_code >= 500,
            ...
        ),
        ...
    )

return OperationExecutionResult(
    outcome=ExecutionOutcome.EXECUTED,
    status_code=response.status_code,
    data=data,
    ...
)
```

Uma response `503` com JSON preserva o JSON em `data` e fica `retryable=True`. Uma response `200`
com texto não JSON vira `INVALID_JSON_RESPONSE`. Antes de o envelope sair, `_redact()` percorre
objetos e listas: chaves declaradas em `redact_fields` e valores de credenciais refletidos pelo
upstream viram `[REDACTED]`.

---

# Parte VII — As funções auxiliares, uma por uma

## 33. `_normalize_base_url()`

[`_normalize_base_url()`](../apps/api/src/indusguard_api/executor.py#L64-L90) usa `httpx.URL` para
interpretar a entrada e exige:

- esquema `http` ou `https`;
- host presente;
- URL absoluta;
- nenhuma credencial embutida;
- nenhuma query;
- nenhum fragmento.

Exemplos:

| URL | Resultado |
|---|---|
| `https://api.example.com` | potencialmente válida |
| `ftp://api.example.com` | inválida |
| `https://user:pass@example.com` | inválida |
| `https://api.example.com?token=abc` | inválida |
| `/api` | inválida |

Depois, a URL normalizada ainda precisa pertencer à allowlist.

## 34. `_serialize_primitive()`

[`_serialize_primitive()`](../apps/api/src/indusguard_api/executor.py#L93-L103):

```python
if value is None or isinstance(value, (dict, list)):
    raise ExecutionValidationError(...)
if isinstance(value, bool):
    return "true" if value else "false"
return str(value)
```

Ela aceita valores simples. Objetos e listas precisam de regras específicas da posição HTTP.

Booleanos viram `true` e `false` minúsculos, forma esperada em serialização JSON/OpenAPI.

## 35. `_validate_schema()` e `$ref` dentro de schemas

[`_validate_schema()`](../apps/api/src/indusguard_api/executor.py#L106-L142) precisa validar tanto
schemas simples quanto referências aninhadas:

```python
validation_document = deepcopy(dict(reference_document))
validation_document["$schema"] = "https://json-schema.org/draft/2020-12/schema"
validation_document["allOf"] = [deepcopy(dict(schema))]

validator_class = validator_for(validation_document)
validator_class.check_schema(validation_document)
errors = list(validator_class(validation_document).iter_errors(value))
```

Por que copiar o OpenAPI completo?

Um schema pode conter:

```yaml
$ref: '#/components/schemas/ActionRequest'
```

Se validássemos somente esse fragmento, `components` não existiria na raiz. Ao manter o documento
completo como raiz e colocar o schema-alvo em `allOf`, os `$ref` continuam encontrando
`components.schemas`.

O código não devolve a mensagem completa do `jsonschema`, pois ela pode reproduzir o valor inválido
e vazar dado pessoal ou segredo.

## 36. `_parameter_definitions()` e `_check_argument_names()`

Essas funções fazem duas validações diferentes:

1. quais parâmetros o contrato declara;
2. quais argumentos o request forneceu.

Exemplo:

```text
declarados: assetId obrigatório
fornecidos: assetId + admin
                         ↓
UNEXPECTED_PATH_ARGUMENT: admin
```

Ou:

```text
declarados: assetId obrigatório
fornecidos: nenhum
               ↓
MISSING_PATH_ARGUMENT: assetId
```

Isso é mais seguro que ignorar silenciosamente valores extras.

## 37. `_render_path()`

[`_render_path()`](../apps/api/src/indusguard_api/executor.py#L196-L223):

```python
template = resolved.operation.path
placeholders = set(PATH_PLACEHOLDER.findall(template))
definitions = _parameter_definitions(resolved, "path")

if placeholders != set(definitions):
    raise ExecutionValidationError("INVALID_OPERATION_CONTRACT", ...)

encoded = quote(_serialize_primitive(value, location="path"), safe="")
rendered = rendered.replace(f"{{{name}}}", encoded)
```

Além de validar o tipo, a função faz percent-encoding.

```text
valor: widget/child
path sem proteção: /widgets/widget/child
path protegido:    /widgets/widget%2Fchild
```

A barra permanece parte do identificador; não cria um novo segmento.

## 38. `_build_query()`

[`_build_query()`](../apps/api/src/indusguard_api/executor.py#L253-L272) valida cada valor e delega a
serialização.

O subconjunto atual aceita `style: form`:

```text
explode=true:
labels=critical&labels=monitored

explode=false:
labels=critical,monitored
```

Objetos em query e outros estilos são bloqueados por enquanto.

## 39. `_build_headers()`

Headers são case-insensitive. Portanto:

```text
X-Request-ID
x-request-id
```

representam o mesmo nome. O executor normaliza para detectar duplicidade e depois preserva a grafia
canônica declarada no OpenAPI.

Valores com `\r` ou `\n` são bloqueados, evitando que um valor tente criar outra linha de header.

## 40. `_build_auth_material()`

[`_build_auth_material()`](../apps/api/src/indusguard_api/executor.py) implementa cinco modos:

| Tipo | Origem | Transporte |
|---|---|---|
| `none` | nenhum valor | nenhum |
| `context_header` | contexto validado | header configurado |
| `api_key_header` | variável indicada por `env` | header configurado |
| `api_key_query` | variável indicada por `env` | query reservada |
| `bearer` | variável indicada por `env` | `Authorization: Bearer ...` |

Para Tractian:

```text
profile.auth.context_field = user_id
request.context.user_id    = usr_001
profile.auth.name          = x-user-id
                              ↓
header final               = x-user-id: usr_001
```

Se o request tentar enviar `x-user-id`, uma API key ou `Authorization` em argumentos, ocorre
`RESERVED_AUTH_HEADER` ou `RESERVED_AUTH_QUERY`.

Por que? Porque identidade e credencial não devem ser argumentos livres escolhidos pelo futuro
agente. Em uma simulação, `include_secrets=False` garante que API keys e tokens nem sequer sejam
lidos do ambiente.

## 41. `_build_body()`

[`_build_body()`](../apps/api/src/indusguard_api/executor.py#L391-L429) verifica quatro casos:

| Contrato | Entrada | Resultado |
|---|---|---|
| sem body | body omitido | `NO_BODY` |
| sem body | body enviado | `UNEXPECTED_REQUEST_BODY` |
| body obrigatório | body omitido | `MISSING_REQUEST_BODY` |
| body declarado | body enviado | valida schema |

A presença explícita é detectada por:

```python
supplied = "body" in arguments.model_fields_set
```

Isso preserva a diferença entre campo omitido e `body=None`.

---

# Parte VIII — Execução completa de `getAsset`

## 42. Todos os dados que entram na composição

### Do OpenAPI Tractian

```text
method       = GET
path         = /assets/{assetId}
assetId      = parâmetro path obrigatório string
seed         = parâmetro query opcional string, resolvido por $ref
```

### Do profile IndusGuard

```text
enabled      = true
timeout      = 10
base_url_env = TRACTIAN_API_URL
allowlist    = [http://localhost:8000]
auth         = context.user_id -> header x-user-id
```

### Do request interno

```text
connector_id = tractian
operation_id = getAsset
assetId      = asset_M101
seed         = case-01
user_id      = usr_001
```

## 43. Transformação passo a passo

```mermaid
sequenceDiagram
    participant R as Request interno
    participant C as ConnectorCatalog
    participant E as HttpExecutor
    participant H as HTTPX
    participant T as API Tractian/Mock

    R->>C: resolve_operation(tractian, getAsset)
    C-->>E: método, path, params, profile e schemas
    E->>E: assetId válido -> /assets/asset_M101
    E->>E: seed válido -> ?seed=case-01
    E->>E: context.user_id -> x-user-id: usr_001
    E->>E: URL do ambiente pertence à allowlist
    E->>H: GET + URL + query + headers + timeout
    H->>T: request HTTP
    T-->>H: 200 + JSON
    H-->>E: httpx.Response
    E-->>R: OperationExecutionResult(EXECUTED)
```

O request HTTP final é:

```http
GET http://localhost:8000/assets/asset_M101?seed=case-01
x-user-id: usr_001
```

## 44. O teste que prova essa ligação

O teste real está em
[`test_executes_tractian_get_with_ref_query_and_context_auth()`](../apps/api/tests/test_executor.py#L421-L447):

```python
def handler(request: httpx.Request) -> httpx.Response:
    assert request.method == "GET"
    assert request.url == httpx.URL(
        "http://localhost:8000/assets/asset_M101?seed=case-01"
    )
    assert request.headers["x-user-id"] == "usr_001"
    return httpx.Response(
        200,
        json={"status": "complete", "data": {"id": "asset_M101"}},
    )

result = _execute(
    catalog,
    _request(
        connector_id="tractian",
        operation_id="getAsset",
        path={"assetId": "asset_M101"},
        query={"seed": "case-01"},
        context={"user_id": "usr_001"},
    ),
    handler,
    environment={"TRACTIAN_API_URL": "http://localhost:8000"},
)
```

O `handler` funciona como API falsa. Se método, URL ou header estiverem incorretos, um `assert`
falha.

## 45. Por que o teste não acessa a internet?

O helper em [`test_executor.py`](../apps/api/tests/test_executor.py#L60-L82) cria:

```python
transport = httpx.MockTransport(handler)

async with httpx.AsyncClient(transport=transport) as client:
    executor = HttpExecutor(
        catalog,
        environment=resolved_environment,
        client=client,
    )
    return await executor.execute(request)
```

O `AsyncClient` pensa que enviará um request, mas `MockTransport` entrega o objeto ao `handler` em
memória. O teste observa exatamente o que chegaria à rede.

## 46. Laboratório executável criado para esta aula

Abra
[`apps/api/examples/executor_walkthrough.py`](../apps/api/examples/executor_walkthrough.py). Ele usa
o catálogo e o executor reais, com `MockTransport`.

Execute a partir da raiz:

```bash
PYTHONPATH=apps/api/src \
  .venv/bin/python apps/api/examples/executor_walkthrough.py
```

Você verá três blocos:

1. `OperationExecutionRequest` recebido;
2. método, URL e identidade preparados pelo executor;
3. `OperationExecutionResult` devolvido.

O JSON impresso mostra `body: null` porque `model_dump_json()` inclui o valor default. A linha
`campos de arguments fornecidos explicitamente` mostra que `body` não pertence a
`model_fields_set`; portanto, para `_build_body()`, ele foi omitido, não enviado como JSON `null`.

Experimentos sugeridos:

1. troque `asset_M101` por `asset/child` e observe `%2F`;
2. remova `user_id` e observe `AUTH_CONTEXT_MISSING`;
3. troque a URL do ambiente e observe `BASE_URL_NOT_ALLOWED`;
4. troque `getAsset` por uma operação inexistente;
5. troque para `updateAssetConfig`, forneça body válido e observe `SIMULATED`, `attempts=0` e a
   ausência de URL externa na prévia.

---

# Parte IX — Caminhos de erro

## 47. `blocked`: regra local impediu a rede

Exemplos:

| Código | Causa |
|---|---|
| `CONNECTOR_NOT_FOUND` | conector desconhecido |
| `OPERATION_NOT_FOUND` | operação desconhecida |
| `OPERATION_DISABLED` | não habilitada no profile |
| `MISSING_PATH_ARGUMENT` | path obrigatório ausente |
| `UNEXPECTED_QUERY_ARGUMENT` | query não declarada |
| `INVALID_REQUEST_BODY` | body viola JSON Schema |
| `AUTH_CONTEXT_MISSING` | contexto não possui identidade |
| `RESERVED_AUTH_HEADER` | tentativa de fornecer auth nos argumentos |
| `AUTH_ENV_MISSING` | variável de credencial não configurada |
| `BASE_URL_NOT_ALLOWED` | destino não pertence à allowlist |
| `METHOD_NOT_SUPPORTED` | método de leitura diferente de GET |
| `WRITE_POLICY_REQUIRED` | modo execute tentou escrita sem policy engine |

Um teste comum usa um contador:

```python
network_calls = 0

def handler(_: httpx.Request) -> httpx.Response:
    nonlocal network_calls
    network_calls += 1
    return httpx.Response(200, json={})

assert result.outcome is ExecutionOutcome.BLOCKED
assert network_calls == 0
```

`network_calls == 0` prova que o bloqueio ocorreu antes do transporte.

## 48. `failed`: a operação podia sair, mas algo falhou

| Código | Causa | Repetível? |
|---|---|---:|
| `UPSTREAM_TIMEOUT` | tempo excedido | sim |
| `UPSTREAM_CONNECTION_ERROR` | conexão falhou | sim |
| `INVALID_JSON_RESPONSE` | body não é JSON | não por default |
| `UPSTREAM_HTTP_ERROR` | status fora de 2xx | 429 ou 5xx |

`retryable=True` informa que a categoria permite repetição. O executor realiza até `max_retries`
novas tentativas somente quando `idempotent=true`; `attempts` registra o total efetivo.

## 49. `executed` não significa “verdade de negócio completa”

Na fixture Tractian, o estado `unavailable` pode chegar dentro de um HTTP 200:

```json
{
  "status": "unavailable",
  "data": null
}
```

Para o executor HTTP, isso é `executed`, porque o transporte funcionou e o upstream respondeu 2xx
com JSON válido.

Interpretar `status="unavailable"` como evidência insuficiente pertence a uma futura camada de
domínio/agente. Essa particularidade está documentada em
[`stakeholder-material.md`](stakeholder-material.md#indisponibilidade-não-é-erro-http).

---

# Parte X — Segurança: por que não é apenas `client.get()`

## 50. Sem URL livre

Inseguro:

```python
await client.get(request.url)
```

Uma entrada poderia transformar o servidor em proxy para destinos arbitrários.

O projeto usa:

```text
connector_id + operation_id
        ↓ catálogo
path conhecido + URL de ambiente aprovada
```

## 51. Negação por default

Uma operação encontrada no OpenAPI, mas ausente do profile, recebe `enabled=False`.

Se um fornecedor adicionar amanhã `DELETE /accounts/{id}`, atualizar o OpenAPI não libera
automaticamente o DELETE.

## 52. Path como dado, não estrutura

`quote(..., safe="")` garante que `/` dentro do identificador vire `%2F`.

## 53. Autenticação fora dos argumentos

O header reservado vem do contexto, não de `arguments.headers`. O futuro agente não escolhe qual
usuário representar.

## 54. Allowlist de destino

Variável de ambiente sozinha não basta. Ela precisa coincidir com um destino aprovado no profile.

## 55. Contrato antes da rede

Ausência, excesso, tipos, enums, body e estilos são validados localmente. A API externa não é usada
como primeira linha de validação.

## 56. Mensagens seguras

Erros não reproduzem:

- o valor inválido de JSON Schema;
- a URL rejeitada;
- a mensagem interna de timeout/conexão.

## 57. Escritas simuladas permanecem fora da rede

No modo default, uma escrita habilitada e válida retorna uma prévia redigida. No modo `execute`, ela
é bloqueada por `WRITE_POLICY_REQUIRED`. Habilitar no profile é necessário, mas não substitui a
futura decisão da policy engine.

---

# Parte XI — O que existe e o que ainda não existe

## 58. Implementado hoje

- descoberta automática de conectores;
- validação YAML/OpenAPI;
- rejeição de chave YAML duplicada;
- resolução local de `$ref` de parâmetros e schemas;
- catálogo com política default segura;
- modelos Pydantic de execução;
- validação de path, query, headers e body;
- serialização previsível de path, query e headers;
- autenticação `none`, `context_header`, API key em header/query e Bearer;
- URL de ambiente + allowlist;
- transporte somente GET;
- timeout;
- retry condicionado por idempotência;
- simulação de escrita sem rede;
- redaction recursiva de campos e credenciais;
- envelope com `attempts` e `SimulatedAction`;
- testes com `MockTransport`.

## 59. Ainda não implementado

- rota FastAPI que chama o executor;
- agente ou LLM;
- MCP;
- policy engine completa;
- escrita real;
- validação do JSON de resposta contra o schema OpenAPI da response;
- interpretação semântica de `complete`, `partial`, `inconclusive`, `conflict` e `unavailable`;
- banco, observabilidade e frontend.

## 60. Campos modelados não são capacidades ativas

O profile contém:

- `risk`;
- `permission`;
- `requires_direct_request`;
- `requires_confirmation`;
- `max_retries`;
- `idempotent`;
- `redact_fields`.

O executor atual aplica `enabled`, timeout, `max_retries`, `idempotent`, `redact_fields`, auth e
allowlist. `risk`, `permission`, `requires_direct_request` e `requires_confirmation` permanecem
reservados para a policy engine. Não interprete presença no schema como execução completa da regra.

---

# Parte XII — Como estudar e depurar

## 61. Primeira sessão: veja funcionando

Execute:

```bash
PYTHONPATH=apps/api/src \
  .venv/bin/python apps/api/examples/executor_walkthrough.py
```

Depois responda:

1. quais campos estavam no request interno?
2. quais campos foram descobertos no catálogo?
3. qual foi a primeira função capaz de chamar a rede?

## 62. Segunda sessão: siga `getAsset`

Abra lado a lado:

1. [`openapi.yaml`](../connectors/tractian/openapi.yaml#L331-L346);
2. [`profile.yaml`](../connectors/tractian/profile.yaml#L7-L40);
3. [`domain.yaml`](../connectors/tractian/domain.yaml#L5-L25);
4. [`OperationExecutionRequest`](../apps/api/src/indusguard_api/schemas.py#L178-L188);
5. [`HttpExecutor.execute()`](../apps/api/src/indusguard_api/executor.py#L446-L551);
6. [teste de `getAsset`](../apps/api/tests/test_executor.py#L421-L447).

Tente desenhar a origem de cada valor do request HTTP final.

## 63. Terceira sessão: use breakpoints

Coloque breakpoints nestes pontos:

1. início de `HttpExecutor.execute()`;
2. retorno de `resolve_operation()`;
3. fim de `_prepare_request()`;
4. início de `_send_request()`;
5. criação do `OperationExecutionResult`.

Inspecione:

- `request`;
- `resolved.operation`;
- `resolved.profile`;
- `prepared`;
- `response`;
- `result`.

## 64. Quarta sessão: rode testes pequenos

```bash
.venv/bin/pytest apps/api/tests/test_executor.py -q -k "tractian_get"
.venv/bin/pytest apps/api/tests/test_executor.py -q -k "invalid_path"
.venv/bin/pytest apps/api/tests/test_executor.py -q -k "base_url"
.venv/bin/pytest apps/api/tests/test_executor.py -q -k "body"
.venv/bin/pytest apps/api/tests/test_executor.py -q -k "authentication"
```

Depois rode a suíte inteira:

```bash
.venv/bin/pytest apps/api/tests/test_executor.py -q
```

## 65. Mapa de leitura do código

| Ordem | Arquivo/trecho | Pergunta respondida |
|---:|---|---|
| 1 | [`schemas.py`](../apps/api/src/indusguard_api/schemas.py#L160-L208) | Qual é a entrada e a saída? |
| 2 | [`getAsset` OpenAPI](../connectors/tractian/openapi.yaml#L331-L346) | Qual chamada técnica existe? |
| 3 | [`profile.yaml`](../connectors/tractian/profile.yaml#L7-L40) | Quais regras locais se aplicam? |
| 4 | [`resolve_operation()`](../apps/api/src/indusguard_api/connectors.py#L498-L522) | O que o catálogo entrega? |
| 5 | [`execute()`](../apps/api/src/indusguard_api/executor.py#L446-L551) | Qual é a ordem das barreiras? |
| 6 | [`_prepare_request()`](../apps/api/src/indusguard_api/executor.py#L553-L570) | Como as partes HTTP são compiladas? |
| 7 | [`_send_request()`](../apps/api/src/indusguard_api/executor.py#L603-L625) | Onde a rede começa? |
| 8 | [`test_executor.py`](../apps/api/tests/test_executor.py#L421-L447) | Como provar o request final? |

---

# Parte XIII — Exercícios

## 66. Perguntas conceituais

1. Por que o OpenAPI não é autorização?
2. Por que o request interno não recebe uma URL?
3. Qual a diferença entre `profile.yaml` e `domain.yaml`?
4. Por que `getAsset` pode ser executado, mas `updateAssetConfig` não?
5. Onde o método `GET` é descoberto?
6. Onde o timeout de 10 segundos é descoberto?
7. Onde o valor `usr_001` é descoberto?
8. Onde o nome `x-user-id` é descoberto?
9. Qual função é a fronteira de rede?
10. Por que HTTP 503 é `failed`, mas operação inexistente é `blocked`?

## 67. Preveja o resultado

### Caso A

```python
path={}
```

Para `getAsset`, qual erro ocorre? A rede é acessada?

### Caso B

```python
path={"assetId": 123}
```

O OpenAPI exige string. Qual categoria de erro ocorre?

### Caso C

```python
path={"assetId": "asset/child"}
```

Qual será o path final?

### Caso D

```python
headers={"x-user-id": "admin"}
context={"user_id": "usr_001"}
```

Qual barreira é acionada?

### Caso E

```python
environment={"TRACTIAN_API_URL": "https://example.invalid"}
```

Por que variável de ambiente não basta?

### Caso F

O upstream responde:

```http
HTTP/1.1 200 OK
Content-Type: text/plain

hello
```

Qual outcome e código são esperados?

<details>
<summary>Gabarito</summary>

1. Conceituais:
   1. OpenAPI descreve capacidade técnica; autorização é decisão local do profile.
   2. Para impedir que a execução vire um proxy para destinos arbitrários.
   3. Profile guarda política/conexão; domain guarda contexto, idioma e significado do domínio.
   4. O transporte externo atual aceita GET; PATCH vira prévia em `simulate` e é bloqueado por
      `WRITE_POLICY_REQUIRED` em `execute`.
   5. No OpenAPI, consolidado em `ResolvedOperation.operation.method`.
   6. No profile, consolidado na política da operação.
   7. Em `request.context`.
   8. Em `profile.auth.name`.
   9. `_send_request()`.
   10. Operação inexistente falha antes da rede; 503 só existe depois de uma chamada permitida.
2. Casos:
   - A: `MISSING_PATH_ARGUMENT`; sem rede.
   - B: `INVALID_PATH_ARGUMENT`; sem rede.
   - C: `/assets/asset%2Fchild`.
   - D: `RESERVED_AUTH_HEADER`; sem rede.
   - E: a URL também precisa pertencer à allowlist; `BASE_URL_NOT_ALLOWED`.
   - F: `failed`, código `INVALID_JSON_RESPONSE`.

</details>

## 68. Desafio de implementação seguro

Sem alterar o comportamento de produção, crie um novo teste que prove:

```text
assetId="a?admin=true"
```

não consegue criar uma query nova pelo path.

Pistas:

- reutilize `test_encodes_path_values_instead_of_creating_new_segments`;
- inspecione `request.url.raw_path`;
- confirme que a query continua vazia;
- não acesse a internet.

---

# Parte XIV — Materiais externos

## 69. Ordem recomendada de leitura

1. [OpenAPI Initiative — What is OpenAPI?](https://www.openapis.org/what-is-openapi)
2. [OpenAPI 3.1 — Path Templating e Parameter Object](https://spec.openapis.org/oas/v3.1.0.html)
3. [JSON Schema — guia de aprendizado](https://json-schema.org/learn)
4. [JSON Schema — `$ref` e organização de schemas](https://json-schema.org/understanding-json-schema/structuring)
5. [Pydantic — Models](https://docs.pydantic.dev/latest/concepts/models/)
6. [HTTPX — suporte assíncrono](https://www.python-httpx.org/async/)
7. [HTTPX — timeouts](https://www.python-httpx.org/advanced/timeouts/)
8. [HTTPX — transports e MockTransport](https://www.python-httpx.org/advanced/transports/)
9. [Python — `urllib.parse.quote`](https://docs.python.org/3/library/urllib.parse.html#urllib.parse.quote)
10. [OWASP — prevenção de SSRF](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)

## 70. Vídeos

1. [Asynchronous Python for the Complete Beginner — Miguel Grinberg, PyCon 2017](https://www.youtube.com/watch?v=iG6fr81xHKA)
2. [Thinking in Coroutines — Łukasz Langa, PyCon 2016](https://pyvideo.org/pycon-us-2016/ukasz-langa-thinking-in-coroutines-pycon-2016.html)
3. [Introduction to OpenAPI Specification — Lorna Mitchell, FOSDEM 2019](https://www.youtube.com/watch?v=EjezAA7YYys)

Os vídeos estão em inglês, mas possuem legendas. Esta aula e os laboratórios do repositório fornecem
o percurso em português.

## 71. Outros documentos do projeto

- [`GUIA_COMPLETO.md`](GUIA_COMPLETO.md): visão do projeto inteiro;
- [`architecture.md`](architecture.md): componentes e fronteiras;
- [`code-guide.md`](code-guide.md): ordem curta de leitura;
- [`stakeholder-material.md`](stakeholder-material.md): análise do pacote fornecido;
- [`connectors/README.md`](../connectors/README.md): como adicionar outro conector.

---

# Parte XV — Checklist de compreensão

Você entendeu o executor atual quando consegue explicar, sem consultar o código:

- o que são request, response, path, query, header e body;
- a diferença entre OpenAPI, profile e domain;
- qual parte veio do contrato Tractian e quais camadas pertencem ao IndusGuard;
- como `getAsset` é encontrado pelo `operationId`;
- como `$ref` de `Seed` é resolvido;
- de onde vêm método, path, timeout, URL e autenticação;
- por que `asset/child` vira `asset%2Fchild`;
- por que `body` ausente difere de `body=None`;
- qual é a primeira função capaz de acessar a rede;
- a diferença entre `blocked`, `failed`, `executed` e `simulated`;
- por que o executor ainda não está ligado a uma rota FastAPI;
- quais campos do profile ainda não são aplicados em runtime;
- quais capacidades ainda precisam ser construídas.

Se algum item ainda estiver nebuloso, volte à execução concreta de `getAsset`, não ao arquivo
inteiro. Uma única chamada entendida de ponta a ponta vale mais que memorizar todas as funções.
