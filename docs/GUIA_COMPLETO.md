# Guia completo do IndusGuard

> Documento único para entender o projeto desde o problema até o código atual.
>
> Última atualização: 17 de agosto de 2026.

## 1. Como usar este guia

Este guia foi escrito para ser lido em ordem. Você não precisa começar abrindo o maior arquivo do
projeto nem precisa entender LangGraph, MCP ou deployment neste momento.

A ordem recomendada é:

1. entender o que estamos construindo;
2. entender o que já existe e o que ainda não existe;
3. aprender o papel de OpenAPI, `profile.yaml` e `domain.yaml`;
4. acompanhar o caminho executado pelo Python;
5. rodar a aplicação e observar as respostas;
6. ler os testes como regras do sistema;
7. estudar o primeiro corte do executor HTTP junto com seus testes.

Se alguma seção parecer abstrata, avance até o laboratório prático e volte depois. Ver o sistema
funcionando costuma tornar os conceitos mais concretos.

---

## 2. O projeto em linguagem simples

O IndusGuard será uma plataforma para agentes de IA que usam APIs externas com segurança.

Imagine que uma pessoa pergunta:

> “O motor está vibrando demais. Verifique os dados e, se for necessário, solicite uma nova
> análise.”

Um agente precisa decidir:

1. qual API consultar;
2. qual endpoint usar;
3. quais argumentos enviar;
4. se possui evidência suficiente;
5. se a pessoa tem permissão;
6. se a ação precisa de confirmação;
7. se deve apenas orientar, executar ou escalar para um humano.

Um LLM não deve controlar sozinho todas essas decisões. Ele é probabilístico: duas execuções podem
produzir planos diferentes. Por isso o IndusGuard colocará uma camada determinística entre o modelo
e a API.

Essa camada dirá, por código:

- quais endpoints existem;
- quais estão habilitados;
- quais são leitura ou escrita;
- qual o risco de cada operação;
- quais permissões são necessárias;
- quando exigir justificativa ou confirmação;
- quais dados devem ser removidos dos traces.

### A frase mais importante deste guia

**OpenAPI informa o que uma API consegue fazer; o profile informa o que o agente tem permissão para
fazer.**

---

## 3. O que já existe e o que ainda não existe

O projeto está sendo construído por camadas.

### Implementado

- monorepo no GitHub;
- backend Python 3.12 com FastAPI;
- configuração por variáveis de ambiente;
- modelos Pydantic para validar profiles;
- descoberta automática de conectores;
- validação OpenAPI 3.x;
- detecção de chaves YAML duplicadas;
- classificação determinística de leitura e escrita;
- conector Tractian com 18 operações;
- conector sintético com 2 operações;
- endpoints de saúde, versão, conectores e operações;
- catálogo com visão pública e metadados internos de execução separados;
- executor interno de operações GET sem autenticação;
- validação JSON Schema dos argumentos de path;
- URL-base por ambiente conferida contra allowlist;
- envelope comum para execução, bloqueio e falha;
- testes automatizados;
- CI com Ruff, pytest e cobertura.

### Ainda não implementado

- autenticação necessária para executar a Tractian;
- parâmetros de query, header e body;
- simulação e execução de escritas;
- retry e redaction em runtime;
- policy engine durante a execução;
- servidor MCP;
- LangGraph;
- chamadas à Groq;
- chat;
- frontend Next.js;
- banco de dados;
- OpenTelemetry;
- benchmark `prompt_only` × `guarded`;
- deployment público.

Portanto, se você iniciar o projeto agora, ele ainda não responderá perguntas industriais nem
chamará um LLM. O executor já consegue transformar `getWidget` em um GET seguro, mas ainda é uma
interface interna exercitada pelos testes e não uma rota do FastAPI.

Isso é intencional. Estamos construindo primeiro a fundação previsível.

---

## 4. Uma analogia para entender a arquitetura

Pense em um restaurante:

| Projeto | Analogia |
|---|---|
| API externa | A cozinha, que realmente produz algo. |
| OpenAPI | O cardápio técnico: pratos, opções e formatos. |
| `profile.yaml` | As regras do restaurante: quem pode pedir, limites e confirmações. |
| `domain.yaml` | O glossário usado pelos atendentes para entender os pedidos. |
| Pydantic | O formulário que rejeita pedidos preenchidos incorretamente. |
| `ConnectorCatalog` | A pessoa que confere cardápio e regras antes de abrir o restaurante. |
| FastAPI | O balcão que expõe as informações já conferidas. |
| Executor atual | O garçom em treinamento que já leva pedidos GET simples até a cozinha. |
| Policy engine futura | O supervisor que aprova ou bloqueia o pedido. |
| Agente futuro | O atendente que conversa e sugere o que pedir. |

Hoje temos cardápio, regras, glossário, conferência, balcão e o primeiro percurso do garçom. Ainda
não implementamos autenticação, ações de escrita nem o atendente inteligente.

---

## 5. Glossário essencial

### API

Uma interface usada por sistemas para trocar dados. Exemplo:

```text
GET /assets/asset_M101
```

Essa chamada pede os dados do ativo `asset_M101`.

### Endpoint

Uma combinação de método HTTP e caminho.

```text
GET   /assets/{assetId}   -> consulta um ativo
PATCH /assets/{assetId}   -> altera um ativo
```

O mesmo caminho pode ter comportamentos diferentes conforme o método.

### OpenAPI

Um documento YAML ou JSON que descreve uma API: endpoints, parâmetros, bodies, schemas e respostas.

Trecho simplificado:

```yaml
paths:
  /assets/{assetId}:
    get:
      operationId: getAsset
      responses:
        "200":
          description: Ativo encontrado
```

### `operationId`

Nome estável de uma operação no OpenAPI. O path pode mudar, mas o sistema usa esse nome para ligar
o endpoint às políticas.

Exemplos:

- `getAsset`;
- `updateAssetConfig`;
- `requestRetraining`.

### YAML

Formato textual de configuração baseado em indentação.

```yaml
enabled: true
risk: high
```

Espaços importam. Tabs não devem ser usados em YAML.

### Conector

Pasta com os arquivos necessários para integrar uma API:

```text
openapi.yaml
profile.yaml
domain.yaml
```

### Catálogo

Representação em memória dos conectores e operações que passaram por todas as validações.

### Pydantic

Biblioteca que transforma dados recebidos em objetos Python tipados e rejeita formatos inválidos.

### FastAPI

Framework que cria endpoints HTTP a partir de funções Python e modelos Pydantic.

### Fail-fast

Falhar imediatamente ao detectar configuração inválida. É melhor impedir o startup do que subir um
serviço com operações faltando ou políticas incorretas.

### Liveness

Responde à pergunta: “o processo HTTP está vivo?”

### Readiness

Responde à pergunta: “o processo terminou de carregar tudo e está pronto para receber tráfego?”

### Idempotência

Uma operação idempotente pode ser repetida sem multiplicar seu efeito. Consultas GET normalmente
são idempotentes. Criar a mesma ordem duas vezes normalmente não é.

### Redaction

Remoção de campos sensíveis antes de armazenar ou exibir traces.

### Drift de contrato

Diferença inesperada entre OpenAPI, profile, backend, frontend ou API real.

---

## 6. Estrutura do repositório

```text
indusguard/
├── .github/
│   └── workflows/
│       └── ci.yml
├── apps/
│   ├── api/
│   │   ├── src/
│   │   │   └── indusguard_api/
│   │   │       ├── __init__.py
│   │   │       ├── settings.py
│   │   │       ├── schemas.py
│   │   │       ├── connectors.py
│   │   │       ├── executor.py
│   │   │       └── main.py
│   │   ├── tests/
│   │   │   ├── conftest.py
│   │   │   ├── test_connectors.py
│   │   │   ├── test_executor.py
│   │   │   └── test_system.py
│   │   └── pyproject.toml
│   └── web/
├── connectors/
│   ├── tractian/
│   │   ├── openapi.yaml
│   │   ├── profile.yaml
│   │   └── domain.yaml
│   └── synthetic/
│       ├── openapi.yaml
│       ├── profile.yaml
│       └── domain.yaml
├── deploy/
├── docs/
├── evals/
├── .env.example
├── Makefile
└── README.md
```

### Pastas que importam agora

- `apps/api/src/indusguard_api`: código executado;
- `apps/api/tests`: regras protegidas pelos testes;
- `connectors`: APIs e políticas carregadas.

### Pastas que podem ser ignoradas por enquanto

- `apps/web`: frontend ainda não iniciado;
- `deploy`: infraestrutura ainda não criada;
- `evals`: runner de avaliação ainda não criado.

---

## 7. Arquitetura atual

```mermaid
flowchart LR
    O[openapi.yaml] --> C[ConnectorCatalog]
    P[profile.yaml] --> C
    D[domain.yaml] --> C
    C --> V[Validação]
    V --> M[Catálogo em memória]
    M --> F[FastAPI]
    F --> R[Endpoints de inspeção]

    M --> E[Executor GET interno]
    E -. depois .-> MCP[Tools MCP]
    MCP -. depois .-> A[Agente LangGraph]
```

Linhas contínuas representam o que existe hoje. O executor ainda não possui rota pública. Linhas
tracejadas representam etapas futuras.

---

## 8. Os três arquivos de um conector

### 8.1 `openapi.yaml`: capacidade técnica

O OpenAPI responde:

- qual método HTTP usar;
- qual o path;
- quais parâmetros existem;
- qual JSON enviar;
- qual resposta esperar.

Exemplo sintético:

```yaml
openapi: 3.1.0
info:
  title: Widget API
  version: 0.1.0

paths:
  /widgets/{widgetId}:
    get:
      operationId: getWidget
      parameters:
        - name: widgetId
          in: path
          required: true
          schema:
            type: string
      responses:
        "200":
          description: Widget encontrado
```

Esse arquivo não diz se o agente deve ter permissão para usar `getWidget`. Ele só diz que a API
oferece essa operação.

#### Restrições atuais do IndusGuard

O loader aceita:

- OpenAPI 3.0 e 3.1;
- APIs REST;
- request e response JSON;
- referências locais como `#/components/schemas/Widget`.

O loader recusa:

- OpenAPI 2;
- `$ref` para URL ou outro arquivo;
- conteúdo binário;
- upload;
- respostas não JSON;
- GraphQL;
- gRPC;
- WebSocket;
- OAuth interativo.

Essas restrições mantêm a primeira versão pequena e segura.

### 8.2 `profile.yaml`: política local

O profile responde:

- qual o ID do conector;
- onde encontrar o OpenAPI;
- de qual variável vem a URL;
- quais URLs são permitidas;
- como autenticar;
- quais operações estão habilitadas;
- quais permissões e confirmações são exigidas.

Exemplo:

```yaml
id: synthetic
name: API sintética
description: API usada para testar a arquitetura
openapi: ./openapi.yaml

base_url_env: SYNTHETIC_API_URL
allowed_base_urls:
  - http://localhost:9000

auth:
  type: none

operations:
  getWidget:
    enabled: true
    access: read
    risk: low
    timeout_seconds: 5
    max_retries: 2
    idempotent: true

  updateWidget:
    enabled: true
    access: write
    risk: high
    permission: action_high
    requires_direct_request: true
    requires_confirmation: true
    justification_min_length: 20
```

#### Campos de operação

| Campo | Significado |
|---|---|
| `enabled` | Informa se o agente poderá usar a operação. |
| `access` | `read` ou `write`. |
| `risk` | `low`, `medium`, `high` ou `critical`. |
| `permission` | Permissão de domínio necessária. |
| `requires_direct_request` | A pessoa deve ter solicitado a ação explicitamente. |
| `requires_confirmation` | A ação precisa de confirmação adicional. |
| `justification_min_length` | Tamanho mínimo da justificativa. |
| `timeout_seconds` | Limite de espera da chamada futura. |
| `max_retries` | Quantidade máxima de novas tentativas. |
| `idempotent` | Informa se repetir é seguro. |
| `redact_fields` | Campos que deverão desaparecer dos traces. |

#### Por que `enabled` começa como `false`?

Imagine que uma API externa adicione amanhã:

```text
DELETE /users/{id}
```

Se o OpenAPI for atualizado automaticamente, o agente não deve ganhar acesso ao DELETE apenas
porque o endpoint apareceu. Alguém precisa revisar e habilitar a operação conscientemente.

#### Tipos de autenticação suportados pelo schema

| Tipo | Uso futuro |
|---|---|
| `none` | API sem autenticação. |
| `api_key_header` | API key em um header. |
| `api_key_query` | API key na query string. |
| `bearer` | Bearer token no header Authorization. |
| `context_header` | Header criado a partir do contexto, como `user_id`. |

Os schemas já validam essas opções. O corte atual aceita somente `none`; os demais modos serão
aplicados no próximo incremento do executor.

### 8.3 `domain.yaml`: significado do domínio

O domain responde:

- qual o idioma principal;
- quais campos formam o contexto;
- qual o significado dos termos;
- quais intenções existem;
- quais operações ajudam cada intenção.

Exemplo:

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
  baseline: estado normal aprendido para um ativo

intents:
  - id: investigar
    description: Consultar sinais, análises e qualidade
    evidence_operations:
      - getAnalysis
      - getBaseline
      - getDataQuality
```

Hoje o loader valida principalmente `context_fields`. Terminologia e intenções serão consumidas
pelo agente e pela interface em etapas futuras.

---

## 9. O backend Python, arquivo por arquivo

Leia os arquivos nesta ordem:

```text
settings.py -> schemas.py -> connectors.py -> main.py -> tests
```

### 9.1 `__init__.py`

Contém a versão pública:

```python
__version__ = "0.1.0"
```

O endpoint `/version` usa essa mesma variável. Ter uma fonte única evita que o pacote diga uma
versão e a API diga outra.

### 9.2 `settings.py`

Responsabilidade: obter configurações do ambiente.

Versão simplificada:

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="INDUSGUARD_",
        extra="ignore",
    )

    environment: str = "development"
    execution_mode: Literal["simulate", "execute"] = "simulate"
    connectors_dir: Path = Field(default_factory=default_connectors_dir)
    api_prefix: str = "/api/v1"
```

#### Como o prefixo funciona

O campo Python:

```text
execution_mode
```

é configurado pela variável:

```text
INDUSGUARD_EXECUTION_MODE
```

#### Por que o default é `simulate`?

Uma instalação nova nunca deve executar mutações reais por acidente. Será necessário escolher
`execute` conscientemente em um ambiente controlado.

#### Por que calcular `connectors_dir` a partir do arquivo?

Para não depender do diretório em que o terminal foi aberto. O código localiza a raiz do monorepo
a partir da posição de `settings.py`.

### 9.3 `schemas.py`

Responsabilidade: declarar os formatos válidos usando Pydantic.

#### `AccessMode`

```python
class AccessMode(StrEnum):
    READ = "read"
    WRITE = "write"
```

Evita strings arbitrárias como `reading`, `edit` ou `mutation`.

#### `RiskLevel`

```python
class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
```

Será usado pela policy engine para decidir confirmação, bloqueio e release gates.

#### `AuthProfile`

Valida autenticação.

Exemplos de regras:

- API key exige `name` e `env`;
- Bearer exige `env`;
- header de contexto exige `name` e `context_field`.

Se faltar um desses campos, o conector não carrega.

#### `OperationPolicy`

Representa as regras de uma operação. Seus defaults são conservadores:

```python
enabled = False
timeout_seconds = 10
max_retries = 0
idempotent = False
```

Os limites Pydantic também impedem:

- timeout acima de 60 segundos;
- mais de 2 retries;
- justificativa negativa ou exageradamente grande.

#### `ConnectorProfile`

Representa todo o `profile.yaml`.

O `id` precisa corresponder ao padrão:

```text
^[a-z][a-z0-9_-]*$
```

Portanto:

- `tractian` é válido;
- `my_api` é válido;
- `2api` é inválido;
- `Minha API` é inválido.

#### `extra="forbid"`

Os profiles rejeitam campos desconhecidos.

Se alguém escrever:

```yaml
max_retry: 2
```

em vez de:

```yaml
max_retries: 2
```

o startup falha. Isso evita ignorar um erro de digitação que poderia alterar segurança.

#### Modelos de resposta

- `OperationSummary`: operação consolidada;
- `ConnectorSummary`: resumo sem operações;
- `ConnectorDetails`: resumo com operações;
- `HealthResponse`: resposta de liveness;
- `ReadyResponse`: resposta de readiness;
- `VersionResponse`: versão e modo.

### 9.4 `connectors.py`

Este é o principal arquivo da etapa atual.

Responsabilidade: transformar arquivos declarativos em catálogo validado.

#### Constantes de métodos HTTP

```python
HTTP_METHODS = {
    "get", "post", "put", "patch",
    "delete", "head", "options", "trace"
}

READ_METHODS = {"get", "head", "options"}
```

Um path OpenAPI também pode conter campos como `parameters`. A lista `HTTP_METHODS` impede que o
loader trate esses campos como endpoints.

#### `ConnectorValidationError`

Erro próprio do domínio. Em vez de espalhar erros de YAML, Pydantic e OpenAPI, o módulo apresenta
mensagens relacionadas à configuração do conector.

#### `UniqueKeyLoader`

O comportamento comum de muitos parsers YAML é conservar apenas a última chave repetida.

Exemplo problemático:

```yaml
paths:
  /assets/{assetId}:
    get: {}

  /assets/{assetId}:
    patch: {}
```

Um parser pode apagar o GET e manter apenas o PATCH. Isso ocorreu no contrato originalmente
fornecido pelos stakeholders.

O `UniqueKeyLoader` percorre o mapa e lança erro quando a chave já existe. Assim o problema aparece
no startup, em vez de uma tool desaparecer silenciosamente.

O contrato Tractian versionado foi normalizado para:

```yaml
paths:
  /assets/{assetId}:
    get: {}
    patch: {}
```

#### `_load_yaml`

Responsabilidades:

1. abrir o arquivo em UTF-8;
2. usar `UniqueKeyLoader`;
3. transformar erros em `ConnectorValidationError`;
4. garantir que a raiz seja um objeto YAML.

#### `_walk`

Percorre recursivamente mapas e listas.

É usado para procurar `$ref`, `format: binary` ou `format: byte` em qualquer nível do OpenAPI.

#### `_validate_runtime_constraints`

Executa duas famílias de validação.

##### Regras do IndusGuard

- OpenAPI precisa começar com `3.`;
- `$ref` precisa começar com `#/`;
- formatos binários são proibidos;
- content types precisam ser JSON.

##### Validação oficial do OpenAPI

Depois das regras locais, `openapi-spec-validator` verifica a estrutura da especificação.

#### `_operation_access`

Classifica automaticamente:

- GET, HEAD e OPTIONS como leitura;
- os demais métodos como escrita.

Essa decisão é código determinístico, não interpretação do LLM.

#### `_operation_risk`

Fornece um default conservador:

- leitura -> risco baixo;
- escrita -> risco alto.

O profile pode declarar risco mais específico.

#### `_build_operation`

Combina duas fontes:

```text
OpenAPI                    profile.yaml
-----------------------    --------------------------
operationId                enabled
método                     risk
path                       permission
summary                    confirmation
tags                       timeout/retry
```

O resultado é um `OperationSummary`.

O método também rejeita contradição como:

```yaml
POST /orders
access: read
```

POST é classificado como escrita, então o conector falha.

#### `_parse_operations`

Percorre todos os paths e métodos do OpenAPI.

Para cada operação:

1. verifica se há `operationId`;
2. rejeita IDs duplicados;
3. procura a política correspondente;
4. usa uma política desabilitada quando ela não existe;
5. constrói o resumo consolidado.

Ao final, também verifica o caminho oposto: se o profile mencionar uma operação que não existe no
OpenAPI, o conector falha.

Isso detecta drift e typos.

#### `ConnectorCatalog`

É a classe que coordena tudo.

Estado interno:

```python
self.connectors_dir
self._connectors
```

##### `load()`

Fluxo:

```text
procura */profile.yaml
    -> carrega conector 1
    -> carrega conector 2
    -> verifica IDs duplicados
    -> substitui o catálogo somente no final
```

O catálogo novo é montado em uma variável temporária. Se o segundo conector falhar, o estado atual
não é substituído por um catálogo pela metade.

Os profiles são ordenados antes da leitura para que Linux, macOS e CI produzam a mesma ordem.

##### `_load_connector()`

Passo a passo:

1. lê `profile.yaml`;
2. valida com `ConnectorProfile`;
3. compara ID e nome da pasta;
4. resolve o caminho do OpenAPI;
5. impede que `../` escape da pasta;
6. lê e valida o OpenAPI;
7. extrai operações;
8. lê `domain.yaml`;
9. valida `context_fields`;
10. constrói `ConnectorDetails`, que é a visão pública;
11. preserva profile e parâmetros em `LoadedConnector`, que é a visão interna.

##### `list()`

Devolve resumos sem operações detalhadas. É usado pela listagem `/connectors`.

##### `get()`

Devolve uma cópia profunda do conector.

Por quê? Se uma rota recebesse o objeto original e alterasse `enabled`, ela corromperia o catálogo
compartilhado por todos os requests seguintes.

##### `resolve_operation()`

Entrega ao executor uma cópia de três elementos:

- profile do conector, incluindo nome da variável de URL e allowlist;
- resumo consolidado da operação;
- parâmetros OpenAPI combinados do path e da operação.

Separar `get()` de `resolve_operation()` evita que as rotas públicas recebam metadados internos de
execução sem necessidade.

### 9.5 `main.py`

Responsabilidade: criar a aplicação FastAPI e expor o catálogo.

#### `create_app`

É uma application factory:

```python
def create_app(*, connectors_dir: Path | None = None) -> FastAPI:
```

Em produção ela usa o diretório configurado. Nos testes ela pode receber uma pasta temporária.

Sem factory, os testes precisariam modificar estado global.

#### Lifespan

```python
@asynccontextmanager
async def lifespan(_: FastAPI):
    catalog.load()
    yield
```

O código antes do `yield` roda no startup. O catálogo precisa carregar antes de a aplicação ficar
pronta.

Se um conector for inválido, o startup falha. Isso é fail-fast.

#### `application.state`

```python
application.state.settings = settings
application.state.connector_catalog = catalog
```

Guarda objetos construídos uma vez para que as rotas não recriem configuração e catálogo em cada
request.

#### Rotas atuais

##### `GET /api/v1/health`

Resposta:

```json
{"status": "healthy"}
```

Confirma que o processo HTTP responde.

##### `GET /api/v1/ready`

Resposta:

```json
{
  "status": "ready",
  "connector_count": 2
}
```

Confirma que o startup terminou e informa quantos conectores carregaram.

##### `GET /api/v1/version`

Resposta:

```json
{
  "version": "0.1.0",
  "environment": "development",
  "execution_mode": "simulate"
}
```

Esses dados serão importantes para correlacionar traces e releases.

##### `GET /api/v1/connectors`

Lista os conectores sem revelar credenciais ou URLs resolvidas.

##### `GET /api/v1/connectors/{connector_id}/operations`

Lista operações consolidadas. Se o conector não existir, retorna 404.

#### Variável global `app`

```python
app = create_app()
```

O Uvicorn procura `indusguard_api.main:app`. A factory continua disponível para os testes.

### 9.6 `executor.py`

Responsabilidade: transformar uma operação conhecida em uma chamada GET validada.

Entrada:

```json
{
  "connector_id": "synthetic",
  "operation_id": "getWidget",
  "arguments": {
    "path": {"widgetId": "widget-123"}
  },
  "context": {}
}
```

Fluxo de `HttpExecutor.execute()`:

1. verifica se conector e operação existem;
2. exige que a operação esteja habilitada;
3. exige método GET e autenticação `none` neste primeiro corte;
4. valida nomes e tipos dos argumentos de path;
5. aplica percent-encoding aos valores;
6. lê a URL-base da variável indicada pelo profile;
7. exige correspondência com `allowed_base_urls`;
8. executa com o timeout da operação;
9. normaliza o resultado.

Os outcomes são:

| Outcome | Significado |
|---|---|
| `executed` | A API respondeu com HTTP 2xx. |
| `blocked` | Uma regra determinística impediu acesso à rede. |
| `failed` | A chamada permitida teve timeout, erro HTTP ou resposta inválida. |
| `simulated` | Reservado para o próximo incremento de escritas. |

O executor recebe `operationId`, não URL. Essa é uma defesa importante contra SSRF: o destino vem
do ambiente e precisa coincidir com a allowlist versionada.

O `httpx.AsyncClient` e o mapa de variáveis de ambiente podem ser injetados. Nos testes, isso
substitui a internet por `httpx.MockTransport` sem criar caminhos especiais no código de produção.

---

## 10. Fluxo completo de startup

```mermaid
sequenceDiagram
    participant U as Uvicorn
    participant A as FastAPI
    participant C as ConnectorCatalog
    participant Y as YAML/OpenAPI

    U->>A: importa main:app
    A->>C: cria catálogo
    U->>A: inicia lifespan
    A->>C: load()
    C->>Y: encontra profiles
    C->>Y: lê e valida arquivos
    C->>C: consolida operações
    alt tudo válido
        C-->>A: 2 conectores
        A-->>U: ready
    else arquivo inválido
        C--xA: ConnectorValidationError
        A--xU: startup falha
    end
```

---

## 11. Testes: regras executáveis do projeto

Comentários e documentação podem ficar desatualizados. Um teste executável reduz esse risco.

### 11.1 `conftest.py`

Cria `ASGITestClient` usando `httpx.ASGITransport`.

Isso permite chamar o FastAPI em memória:

- nenhuma porta é aberta;
- nenhum servidor externo é necessário;
- os testes são rápidos;
- o lifespan continua sendo executado.

Cada teste recebe uma aplicação nova para evitar vazamento de estado.

### 11.2 `test_system.py`

Protege:

1. resposta de health;
2. readiness com dois conectores;
3. default `simulate`.

O terceiro teste é uma regra de segurança: uma instalação nova não começa executando mutações.

### 11.3 `test_connectors.py`

Protege:

1. descoberta de Tractian e synthetic;
2. Tractian com 18 operações;
3. synthetic com 2 operações;
4. presença simultânea de GET e PATCH em `/assets/{assetId}`;
5. permissão e confirmação da escrita de configuração;
6. 404 para conector desconhecido;
7. operações sem profile desabilitadas;
8. rejeição de YAML duplicado.

### 11.4 `test_executor.py`

Seus 16 casos comprovam:

- GET sintético e envelope comum;
- percent-encoding de valores do path;
- ausência, excesso e tipo incorreto de argumentos;
- conector, operação e configuração ausentes;
- operação desabilitada e escrita bloqueadas antes da rede;
- URL fora da allowlist;
- timeout, HTTP 503 e resposta não JSON;
- bloqueio explícito da autenticação Tractian ainda não implementada.

Todos usam `httpx.MockTransport`; nenhum teste acessa a internet.

### 11.5 Estado atual da suíte

- 24 testes;
- 90% de cobertura total;
- Ruff aprovado;
- formatação aprovada;
- 2 conectores válidos.

Cobertura não significa ausência de bugs. Ela informa quais linhas foram exercitadas, não se todos
os comportamentos possíveis estão corretos.

---

## 12. Os conectores incluídos

### 12.1 Synthetic

Objetivo: provar que o núcleo é genérico.

Possui:

- `getWidget`: leitura;
- `updateWidget`: escrita de alto risco.

Nenhum `if connector == "synthetic"` foi adicionado ao Python. O catálogo descobre a pasta.

Essa é a prova arquitetural: uma API pequena entra apenas por OpenAPI + YAML.

### 12.2 Tractian

Objetivo: primeiro domínio industrial realista.

Possui 18 operações nas áreas:

- empresa e usuário;
- ativos;
- análises;
- baseline;
- RMS;
- espectro;
- qualidade dos dados;
- modelos;
- conhecimento;
- ações e escalonamento.

#### Autenticação

```yaml
auth:
  type: context_header
  name: x-user-id
  context_field: user_id
```

No futuro, o executor obterá `user_id` do contexto validado e criará o header `x-user-id`.

O valor não será escolhido livremente pelo modelo.

#### Leituras reutilizam âncora YAML

```yaml
getCompany: &read_operation
  enabled: true
  access: read
  risk: low
  timeout_seconds: 10
  max_retries: 2
  idempotent: true

getAsset: *read_operation
```

`&read_operation` cria uma âncora. `*read_operation` reutiliza o mesmo bloco.

#### Escritas são individuais

Exemplo:

```yaml
updateAssetConfig:
  enabled: true
  access: write
  risk: high
  permission: action_high
  requires_direct_request: true
  requires_confirmation: true
  justification_min_length: 20
```

Escritas não compartilham uma política única porque permissões e riscos variam.

---

## 13. Material fornecido pelos stakeholders

O pacote Tractian × Inteli inclui:

- API FastAPI com 18 operações;
- 12 tabelas Parquet e um `seed.json`;
- 8 empresas;
- 26 ativos;
- 24 análises;
- 17 chamados de entrada;
- 16 cenários oficiais;
- 57 chamadas em trajetórias esperadas;
- 39 testes da API.

### O que foi validado

- os 39 testes fornecidos passaram;
- as 57 chamadas esperadas responderam com sucesso;
- não foram encontrados registros duplicados nos Parquets;
- as chaves estrangeiras principais são válidas.

### Limitações encontradas

#### OpenAPI com path duplicado

O contrato repetia `/assets/{assetId}` para GET e PATCH. Um parser YAML comum encontrou apenas 17
operações porque descartou uma ocorrência.

Nossa cópia une os métodos sob uma chave e possui teste de regressão.

#### Escritas permissivas

A API fornecida valida principalmente permissão e justificativa. Ela aceita alguns dados fora do
domínio, como criticidade inválida.

Consequência: o executor deve validar argumentos pelo OpenAPI antes da chamada.

#### Ausência de isolamento por empresa

Uma pessoa com a permissão certa pode atuar sobre recurso de outra empresa na fixture.

Consequência: a policy engine precisa verificar usuário, empresa e recurso.

#### Gabarito espalhado

Respostas esperadas aparecem em `eval/`, documentação, Parquet de casos e gerador de dados.

Consequência: esses arquivos não podem entrar na imagem ou no contexto do agente.

#### `unavailable` usa HTTP 200

A indisponibilidade aparece dentro do JSON, não como 503.

Consequência: o agente deve interpretar o estado; timeout, 429 e 5xx precisam de mocks.

#### Ações não persistem

`accepted=true` representa sucesso, mas uma consulta posterior pode mostrar o estado antigo.

Consequência: a UI precisa explicar essa limitação da fixture.

#### Inconsistência de contexto

O caso `TKT-EXE-15` relaciona uma pessoa e um ativo de empresas diferentes.

Consequência: não devemos assumir que os dados fornecidos resolvem autorização por nós.

---

## 14. Segurança implementada até agora

### Operações desconhecidas ficam desabilitadas

Evita liberação automática de endpoint novo.

### YAML duplicado é rejeitado

Evita perda silenciosa de operação.

### `$ref` externo é rejeitado

Evita que o loader busque schemas em arquivo ou host fora do conector.

### Conteúdo não JSON é rejeitado

Mantém o escopo da primeira versão e reduz complexidade.

### OpenAPI não pode escapar da pasta

Um profile com `../../arquivo.yaml` é bloqueado.

### Escrita contraditória é rejeitada

Uma operação POST não pode declarar `access: read`.

### Campos desconhecidos são rejeitados

Typos de política não são ignorados.

### Default é simulação

Mesmo quando o executor existir, o modo inicial será seguro.

### Segredos não ficam nos profiles

O YAML guarda somente o nome da variável de ambiente.

---

## 15. Como executar localmente

Abra um terminal na raiz do repositório.

### 15.1 Conferir Python

```bash
python3.12 --version
```

### 15.2 Instalar dependências

```bash
make setup
```

O comando:

1. cria `.venv`;
2. atualiza pip;
3. instala FastAPI, Pydantic e dependências;
4. instala pytest e Ruff.

Não é necessário ativar o ambiente porque o Makefile chama `.venv/bin/...`.

### 15.3 Criar configuração local

```bash
cp .env.example .env
```

O `.env` real é ignorado pelo Git.

### 15.4 Validar conectores

```bash
make validate
```

Saída esperada:

```text
2 conectores válidos
```

### 15.5 Rodar testes

```bash
make test
```

Saída esperada:

```text
........
8 passed
```

### 15.6 Verificar estilo

```bash
make lint
```

### 15.7 Iniciar API

```bash
make dev-api
```

Abra:

```text
http://127.0.0.1:8000/docs
```

---

## 16. Laboratório prático guiado

### Experimento 1: liveness

```bash
curl -s http://127.0.0.1:8000/api/v1/health
```

Observe:

```json
{"status":"healthy"}
```

Pergunta: esse endpoint confirma que os conectores foram carregados?

Resposta: não. Ele confirma apenas que o processo responde.

### Experimento 2: readiness

```bash
curl -s http://127.0.0.1:8000/api/v1/ready
```

Observe:

```json
{"status":"ready","connector_count":2}
```

Agora sabemos que o lifespan terminou e o catálogo possui dois conectores.

### Experimento 3: listar conectores

```bash
curl -s http://127.0.0.1:8000/api/v1/connectors
```

Procure:

- `synthetic`;
- `tractian`;
- versão OpenAPI;
- quantidade de operações;
- tipo de autenticação;
- campos de contexto.

Observe que nenhuma API key é devolvida.

### Experimento 4: comparar OpenAPI e profile

Abra lado a lado:

```text
connectors/synthetic/openapi.yaml
connectors/synthetic/profile.yaml
```

Encontre `getWidget` nos dois arquivos.

No OpenAPI, identifique:

- método GET;
- path `/widgets/{widgetId}`;
- parâmetro `widgetId`.

No profile, identifique:

- `enabled: true`;
- `access: read`;
- `risk: low`;
- retry e idempotência.

Essa comparação é o coração do projeto.

### Experimento 5: observar uma operação consolidada

```bash
curl -s http://127.0.0.1:8000/api/v1/connectors/synthetic/operations
```

Você verá no mesmo objeto dados técnicos e políticos.

Pergunte para cada campo: ele veio do OpenAPI ou do profile?

### Experimento 6: erro 404

```bash
curl -i http://127.0.0.1:8000/api/v1/connectors/inexistente/operations
```

O backend devolve 404 porque lista vazia seria ambígua: poderia significar conector vazio ou ID
incorreto.

### Experimento 7: entender um teste

Abra `apps/api/tests/test_connectors.py` e encontre:

```python
def test_unconfigured_operations_are_disabled(...):
```

O teste cria uma API temporária com GET e POST, mas profile vazio. Depois confirma que todas as
operações estão desabilitadas.

Esse teste não verifica implementação acidental. Ele protege uma regra de segurança.

---

## 17. Integração contínua

O arquivo `.github/workflows/ci.yml` roda em push para `main` e em pull requests.

Fluxo:

```text
checkout
  -> instala Python 3.12
  -> instala dependências
  -> ruff check
  -> ruff format --check
  -> pytest com cobertura
```

Por que isso importa?

Uma mudança pode funcionar no computador de quem escreveu e falhar em outro ambiente. O CI executa
as regras em uma máquina limpa.

---

## 18. Executor HTTP genérico: primeiro corte implementado

O catálogo responde “esta operação existe e possui esta política”. O executor começou a responder
“como chamar essa operação com segurança”.

Fluxo atual:

```mermaid
flowchart LR
    I[operationId + argumentos + contexto] --> C[Consulta catálogo]
    C --> P{Operação habilitada?}
    P -- não --> B[blocked]
    P -- sim --> G{GET e auth none?}
    G -- não --> B
    G -- sim --> J[Validação JSON Schema do path]
    J --> U[URL do ambiente + allowlist]
    U --> H[GET com timeout]
    H --> R[Envelope executed ou failed]
```

### Responsabilidades já implementadas

1. localizar a operação por `connector_id` e `operation_id`;
2. recusar operação desabilitada;
3. aceitar somente GET e autenticação `none`;
4. validar argumentos de path pelo OpenAPI;
5. codificar valores para impedir criação de segmentos extras;
6. conferir a URL-base contra a allowlist;
7. aplicar timeout;
8. normalizar status, dados, erro e latência;
9. não revelar URL rejeitada nem detalhes internos de transporte.

### Limites que permanecem explícitos

- query, headers e body ainda não entram em `ExecutionArguments`;
- `$ref` em parâmetro de path é bloqueado até existir resolvedor local;
- autenticação diferente de `none` retorna `AUTH_NOT_SUPPORTED`;
- métodos diferentes de GET retornam `METHOD_NOT_SUPPORTED`;
- `max_retries` ainda não dispara repetição;
- `redact_fields` ainda não é aplicado ao resultado;
- não existe rota FastAPI de execução.

### Critérios comprovados pelos testes

- GET sintético executado com URL correta;
- valor com barra permanece dado por causa do percent-encoding;
- argumento ausente, extra ou com tipo errado é bloqueado antes da rede;
- operação desconhecida ou desabilitada é bloqueada;
- URL arbitrária é impossível;
- timeout, HTTP 503 e resposta não JSON são normalizados;
- PATCH sintético não chega à rede;
- Tractian não é chamada anonimamente;
- testes anteriores continuam verdes.

### Próximo incremento do executor

1. resolver `$ref` local;
2. adicionar query e headers;
3. adicionar body JSON;
4. implementar `context_header`, API key e Bearer;
5. simular escritas em `EXECUTION_MODE=simulate`;
6. aplicar retry somente quando seguro;
7. aplicar redaction.

Somente depois o executor será exposto como tools MCP ao agente.

---

## 19. Visão das etapas futuras

### Etapa 2: executor

Em andamento. O corte GET + path + allowlist está pronto; faltam autenticação, demais argumentos,
escrita simulada, retry e redaction.

### Etapa 3: policy engine

Avaliar permissão, pedido direto, justificativa, confirmação e contexto.

### Etapa 4: MCP

Disponibilizar operações aprovadas como tools.

### Etapa 5: LangGraph + Groq

Criar o fluxo:

```text
validar solicitação
-> classificar intenção
-> planejar evidências
-> consultar tools
-> propor ação
-> aplicar política
-> executar, bloquear ou escalar
-> responder com evidências
```

### Etapa 6: persistência e observabilidade

Guardar runs redigidas, tool calls, decisões de política, tokens e latência.

### Etapa 7: avaliação

Comparar:

- `prompt_only`: depende principalmente do prompt;
- `guarded`: passa por políticas determinísticas.

Hipótese:

> A camada determinística reduz chamadas inseguras ou sem evidência, sem perder mais de um dos 16
> cenários oficiais e sem adicionar mais de 25% de latência mediana.

### Etapa 8: frontend

Telas planejadas:

- visão do sistema;
- playground;
- trace;
- conectores;
- avaliações;
- system card.

### Etapa 9: deployment

- frontend estático;
- backend Docker;
- banco gratuito;
- OpenTelemetry;
- CI/CD com gates de avaliação.

---

## 20. Plano de estudos sugerido

### Dia 1: conceitos e execução

Objetivo: entender o que o sistema faz hoje.

1. Leia as seções 2 a 8.
2. Rode `make validate`.
3. Rode `make test`.
4. Inicie a API.
5. Execute os três primeiros curls.

Você concluiu quando conseguir explicar a diferença entre OpenAPI, profile e domain.

### Dia 2: Pydantic e configuração

Objetivo: entender como dados inválidos são rejeitados.

1. Leia `settings.py`.
2. Leia os enums de `schemas.py`.
3. Leia `AuthProfile` e `OperationPolicy`.
4. Compare os campos com `connectors/synthetic/profile.yaml`.

Você concluiu quando conseguir explicar `extra="forbid"` e o default `enabled=false`.

### Dia 3: loader de YAML e OpenAPI

Objetivo: entender a fronteira de confiança.

1. Leia o topo de `connectors.py`.
2. Estude `UniqueKeyLoader`.
3. Estude `_load_yaml` e `_walk`.
4. Estude `_validate_runtime_constraints`.

Você concluiu quando conseguir explicar por que `$ref` externo e chave duplicada são bloqueados.

### Dia 4: operações e catálogo

Objetivo: entender a consolidação.

1. Leia `_operation_access`.
2. Leia `_build_operation`.
3. Leia `_parse_operations`.
4. Leia `ConnectorCatalog`.

Você concluiu quando conseguir narrar o fluxo de `profile.yaml` até `OperationSummary`.

### Dia 5: FastAPI

Objetivo: entender startup e endpoints.

1. Leia `create_app`.
2. Leia o lifespan.
3. Entenda `app.state`.
4. Relacione cada rota ao seu response model.

Você concluiu quando conseguir explicar health versus ready.

### Dia 6: testes

Objetivo: interpretar testes como requisitos.

1. Leia `conftest.py`.
2. Leia `test_system.py`.
3. Leia `test_connectors.py`.
4. Para cada teste, escreva em uma frase qual risco ele previne.

Você concluiu quando conseguir explicar por que o teste de operação desabilitada é segurança, não
apenas cobertura.

### Dia 7: estudar o primeiro executor

Objetivo: acompanhar uma execução do contrato até o envelope.

1. Releia a seção 18.
2. Leia `executor.py` ao lado de `test_executor.py`.
3. Desenhe a entrada e a saída do executor.
4. Liste quais erros são `blocked` e quais são `failed`.
5. Explique por que `MockTransport` prova a URL sem acessar a internet.
6. Identifique os pontos em que autenticação e escrita serão acrescentadas.

---

## 21. Perguntas para verificar seu entendimento

Tente responder sem olhar o código.

1. Por que OpenAPI não é autorização?
2. Por que uma operação nova começa desabilitada?
3. Qual é a função de `operationId`?
4. O que `UniqueKeyLoader` evita?
5. Por que o catálogo falha no startup?
6. Por que `ConnectorCatalog.get()` devolve uma cópia?
7. Qual a diferença entre health e ready?
8. Por que o executor atual não chama a Tractian?
9. Onde uma API key real deve ficar?
10. Por que POST não pode declarar `access: read`?
11. Por que o conector synthetic é importante?
12. Por que o gabarito não pode entrar no runtime?
13. O que `simulate` protege?
14. Qual é o papel atual do executor?
15. Em que momento o LangGraph deve ser adicionado?

### Respostas resumidas

1. Porque descreve capacidade técnica, não permissão local.
2. Para impedir liberação automática de endpoints.
3. Ligar endpoint, política, domínio e futura tool.
4. Perda silenciosa de chaves YAML repetidas.
5. Para não anunciar catálogo parcial como saudável.
6. Para consumidores não alterarem estado compartilhado.
7. Health verifica processo; ready depende do startup concluído.
8. Porque `context_header` ainda não foi implementado e chamadas anônimas são bloqueadas.
9. Em variável de ambiente ou secret manager.
10. Porque o método implica mutação.
11. Prova que o núcleo não depende da Tractian.
12. Porque contaminaria a avaliação.
13. Execução acidental de mutações reais.
14. Validar path, URL e timeout, executar GET permitido e devolver envelope comum.
15. Depois que catálogo, executor e política estiverem confiáveis.

---

## 22. Erros comuns de interpretação

### “O conector já chama a API”

Não. O conector descreve e valida. Quem chama é `HttpExecutor`, e somente no subconjunto já
implementado.

### “Se está no OpenAPI, o agente pode usar”

Não. Também precisa estar habilitado no profile.

### “`simulate` já está simulando PATCH”

O modo já existe na configuração, mas a ramificação de escrita ainda não foi implementada. O
executor bloqueia PATCH com `METHOD_NOT_SUPPORTED`; ele não envia nem finge sucesso.

### “O conector synthetic é um mock da Tractian”

Não. Ele representa outro domínio mínimo para testar extensibilidade.

### “90% de cobertura significa sistema 90% correto”

Não. Cobertura mede linhas exercitadas.

### “Já precisamos entender LangGraph”

Não. LangGraph só entrará depois da camada de tools confiáveis.

### “Preciso ler as 641 linhas do OpenAPI Tractian agora”

Não. Comece pelo conector synthetic e depois inspecione operações específicas da Tractian.

---

## 23. Resumo final

O que construímos até agora é uma fundação para agentes conectados a APIs.

O núcleo atual:

1. encontra conectores;
2. valida YAML;
3. valida OpenAPI;
4. valida profiles com Pydantic;
5. separa leitura e escrita;
6. combina endpoints e políticas;
7. cria um catálogo em memória;
8. expõe esse catálogo pelo FastAPI;
9. falha cedo quando encontra inconsistência;
10. resolve operações internas sem expor o profile pelas rotas;
11. valida e executa GET sintético por allowlist;
12. normaliza execução, bloqueio e falha;
13. protege decisões importantes com testes.

O sistema ainda não possui agente, mas já possui catálogo e o primeiro executor. Ele impede que o
futuro agente opere sobre uma lista ambígua e também prova o caminho seguro de uma operação GET até
uma API sem autenticação.

O próximo passo é completar argumentos e autenticação e então simular escritas. Depois disso, MCP e
LangGraph poderão consumir uma interface mais confiável.

Se você guardar apenas três ideias, guarde estas:

1. **OpenAPI descreve capacidade; profile concede permissão.**
2. **O agente será probabilístico, mas as regras de segurança serão determinísticas.**
3. **O executor recebe operationId, nunca uma URL arbitrária.**
