# Guia de leitura do código

Este documento sugere uma ordem para estudar o backend sem precisar entender tudo de uma vez.

## 1. `settings.py`: de onde vêm as configurações

Comece em [`settings.py`](../apps/api/src/indusguard_api/settings.py). A classe `Settings` usa o
prefixo `INDUSGUARD_`, portanto o campo `execution_mode` é configurado por
`INDUSGUARD_EXECUTION_MODE`.

O default é `simulate`: uma mutação aprovada pela policy engine gera uma prévia redigida, sem
resolver a URL externa, ler credenciais do ambiente ou abrir conexão. O modo `execute` continua
bloqueado por `REAL_WRITE_DISABLED`, mesmo depois de confirmação válida.

## 2. `schemas.py`: qual é o formato aceito

[`schemas.py`](../apps/api/src/indusguard_api/schemas.py) contém os modelos Pydantic.

Os mais importantes para começar são:

- `AuthProfile`: formas aceitas de autenticação;
- `OperationPolicy`: regras de uma operação;
- `ConnectorProfile`: formato completo de `profile.yaml`;
- `OperationSummary`: resultado de OpenAPI + política;
- `ConnectorSummary`: visão pública sem segredos.
- `OperationExecutionResult`: envelope com resultado, tentativas e simulação opcional;
- `SimulatedAction`: prévia tipada de uma escrita que não chegou à rede.
- `PolicyPrincipal`: identidade, permissões e escopos confiáveis;
- `PolicyEvaluationRequest`: proposta técnica e sinais usados na avaliação;
- `PolicyDecision`: outcome, códigos, digest e metadados sem dados brutos;
- `GuardedExecutionResult`: decisão e resultado opcional do executor.

Todos os profiles usam `extra="forbid"`. Se alguém escrever `max_retry` em vez de `max_retries`, o
startup falha mostrando o erro. Aceitar silenciosamente esse typo seria perigoso.

## 3. `connectors.py`: como a configuração vira catálogo

O fluxo principal de [`connectors.py`](../apps/api/src/indusguard_api/connectors.py) é:

```text
ConnectorCatalog.load
    └── encontra cada profile.yaml
        └── _load_connector
            ├── _load_yaml(profile)
            ├── valida ConnectorProfile com Pydantic
            ├── _load_yaml(openapi)
            ├── _validate_runtime_constraints
            ├── _parse_operations
            └── lê context_fields do domain.yaml
```

### Por que existe `UniqueKeyLoader`?

YAML permite que parsers encontrem a mesma chave duas vezes, mas muitos mantêm apenas a última. O
contrato original do desafio repetia `/assets/{assetId}` para GET e PATCH. Sem a validação, uma das
operações desapareceria sem erro. O loader próprio transforma essa ambiguidade em falha explícita.

### Por que uma operação não configurada fica desabilitada?

Imagine que a API de um fornecedor publique amanhã `DELETE /accounts/{id}`. O catálogo poderá
descobrir o endpoint, mas não deve entregá-lo automaticamente ao agente. A liberação exige uma
decisão consciente em `profile.yaml`.

### Por que `get()` devolve `deepcopy`?

O catálogo é compartilhado pela aplicação. Se uma rota recebesse o objeto original e alterasse
`enabled`, todos os requests seguintes veriam estado corrompido. A cópia preserva o catálogo.

## 4. `main.py`: como o catálogo chega ao HTTP

[`main.py`](../apps/api/src/indusguard_api/main.py) usa uma application factory chamada
`create_app`. Produzir uma aplicação nova permite apontar os testes para conectores temporários.

O `lifespan` carrega o catálogo uma única vez no startup. As rotas recuperam a instância pronta em
`app.state`.

As rotas atuais não executam a API Tractian; apenas expõem metadados já validados. O executor é
uma interface interna e ainda não foi conectado ao FastAPI.

## 5. `executor.py`: como uma operação vira execução ou simulação

[`executor.py`](../apps/api/src/indusguard_api/executor.py) implementa um corte pequeno e completo:

Para uma aula passo a passo, exemplo completo de `getAsset`, exercícios e materiais externos,
consulte também o [guia de estudo do executor](executor-study-guide.md).

```text
OperationExecutionRequest
    -> localizar conector e operação
    -> conferir enabled
    -> resolver $ref e validar path/query/header/body
    -> separar leitura de escrita
    -> simular escrita sem rede, ou exigir a composição protegida no modo execute
    -> obter autenticação do contexto/ambiente em leituras
    -> resolver e conferir URL-base
    -> executar GET com timeout e retry idempotente
    -> aplicar redaction
    -> OperationExecutionResult
```

Estude primeiro `HttpExecutor.execute()`. Cada retorno antecipado representa uma regra que impede a
rede de ser acessada. Depois acompanhe `_render_path()`, `_build_query()`, `_build_headers()`,
`_build_auth_material()`, `_build_body()` e `_redact()`. Uma barra no path é percent-encoded;
autenticação não pode vir dos argumentos; e um body de escrita é validado antes da simulação.

O catálogo resolve Reference Objects de parâmetros e conserva a raiz OpenAPI no objeto interno.
Durante a validação, essa raiz permite que schemas como
`#/components/schemas/ActionRequest` sejam resolvidos sem arquivos ou rede.

O `environment` e o `httpx.AsyncClient` podem ser injetados. Essa escolha torna o teste
determinístico: uma variável de ambiente falsa e um transporte em memória substituem dependências
externas sem adicionar condições especiais ao código de produção.

Os outcomes têm significados diferentes:

- `executed`: upstream respondeu com HTTP 2xx;
- `blocked`: uma regra determinística interrompeu antes da rede;
- `failed`: uma chamada permitida teve timeout, erro HTTP ou resposta inválida;
- `simulated`: escrita validada e redigida que realizou zero tentativas de rede.

`attempts` vale zero para bloqueio/simulação e informa quantas chamadas ocorreram em execução ou
falha. Retry só usa `max_retries` quando `idempotent=true` e a falha é timeout, conexão, 429 ou 5xx.

## 6. `policy.py`: por que proposta não é autorização

[`policy.py`](../apps/api/src/indusguard_api/policy.py) possui duas classes:

- `PolicyEngine`: avalia somente dados tipados, sem rede e sem LLM;
- `GuardedExecutor`: encaminha ao HTTP apenas `allow` para leitura ou `simulate` para escrita.

Ordem mental da avaliação:

```text
operação conhecida e habilitada
    -> identidade autenticada coincide com o contexto
    -> escopos exigidos existem e são idênticos nas três fontes
    -> principal possui a permissão do profile
    -> ação foi pedida diretamente quando exigido
    -> justificativa existe no JSON Pointer e passa no tamanho mínimo
    -> simular, pedir confirmação ou bloquear execução real
```

O digest de confirmação é calculado com JSON canônico e SHA-256. Ele muda quando operação,
argumentos, contexto, principal ou escopos mudam. A decisão só expõe o hash, nunca esses valores em
texto puro. Confirmação não é exigida para simulação; no modo `execute`, ela é verificada e depois a
escrita ainda é bloqueada pelo limite explícito deste release.

## 7. Testes: qual comportamento não pode regredir

Leia [`test_connectors.py`](../apps/api/tests/test_connectors.py) depois do núcleo. Os testes provam
cinco propriedades arquiteturais:

1. dois conectores são descobertos sem registro manual;
2. GET e PATCH do mesmo path Tractian continuam presentes;
3. conector desconhecido retorna 404;
4. operação sem profile fica desabilitada;
5. YAML duplicado é rejeitado.

[`test_system.py`](../apps/api/tests/test_system.py) cobre liveness, readiness e o default seguro de
simulação. [`test_executor.py`](../apps/api/tests/test_executor.py) usa `httpx.MockTransport` e
conectores temporários para provar URLs, autenticação, retry, redaction e envelopes sem internet.
[`test_policy.py`](../apps/api/tests/test_policy.py) prova identidade, escopos, justificativa,
digest, confirmação e ausência de rede em decisões que interrompem o fluxo.

## 8. O que ainda não procurar no código

Ainda não existem chamada de LLM, MCP, LangGraph, banco ou frontend. Também não há rota pública do
executor nem escrita real. A policy engine já avalia a proposta internamente; o próximo consumidor
planejado é a camada de tools MCP.
