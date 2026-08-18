# Guia de leitura do código

Este documento sugere uma ordem para estudar o backend sem precisar entender tudo de uma vez.

## 1. `settings.py`: de onde vêm as configurações

Comece em [`settings.py`](../apps/api/src/indusguard_api/settings.py). A classe `Settings` usa o
prefixo `INDUSGUARD_`, portanto o campo `execution_mode` é configurado por
`INDUSGUARD_EXECUTION_MODE`.

O default é `simulate`. O primeiro executor ainda bloqueia toda escrita; manter esse default no
contrato prepara a simulação de mutações sem permitir que uma etapa futura escreva por acidente.

## 2. `schemas.py`: qual é o formato aceito

[`schemas.py`](../apps/api/src/indusguard_api/schemas.py) contém os modelos Pydantic.

Os mais importantes para começar são:

- `AuthProfile`: formas aceitas de autenticação;
- `OperationPolicy`: regras de uma operação;
- `ConnectorProfile`: formato completo de `profile.yaml`;
- `OperationSummary`: resultado de OpenAPI + política;
- `ConnectorSummary`: visão pública sem segredos.

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

## 5. `executor.py`: como uma operação vira GET

[`executor.py`](../apps/api/src/indusguard_api/executor.py) implementa um corte pequeno e completo:

```text
OperationExecutionRequest
    -> localizar conector e operação
    -> conferir enabled, GET e auth none
    -> validar argumentos de path
    -> resolver e conferir URL-base
    -> executar com timeout
    -> OperationExecutionResult
```

Estude primeiro `HttpExecutor.execute()`. Cada retorno antecipado representa uma regra que impede a
rede de ser acessada. Depois leia `_render_path()`: além de validar o tipo pelo JSON Schema, ele usa
percent-encoding para uma barra recebida como dado não criar outro segmento de URL.

O `environment` e o `httpx.AsyncClient` podem ser injetados. Essa escolha torna o teste
determinístico: uma variável de ambiente falsa e um transporte em memória substituem dependências
externas sem adicionar condições especiais ao código de produção.

Os outcomes têm significados diferentes:

- `executed`: upstream respondeu com HTTP 2xx;
- `blocked`: uma regra determinística interrompeu antes da rede;
- `failed`: uma chamada permitida teve timeout, erro HTTP ou resposta inválida;
- `simulated`: reservado para o próximo incremento de escrita.

## 6. Testes: qual comportamento não pode regredir

Leia [`test_connectors.py`](../apps/api/tests/test_connectors.py) depois do núcleo. Os testes provam
cinco propriedades arquiteturais:

1. dois conectores são descobertos sem registro manual;
2. GET e PATCH do mesmo path Tractian continuam presentes;
3. conector desconhecido retorna 404;
4. operação sem profile fica desabilitada;
5. YAML duplicado é rejeitado.

[`test_system.py`](../apps/api/tests/test_system.py) cobre liveness, readiness e o default seguro de
simulação. [`test_executor.py`](../apps/api/tests/test_executor.py) usa `httpx.MockTransport` para
provar que URLs e envelopes estão corretos sem acessar a internet.

## 7. O que ainda não procurar no código

Ainda não existem chamada de LLM, MCP, LangGraph, banco ou frontend. O executor atual também não
possui query, body, autenticação, retry ou escrita simulada. Esses limites são bloqueios explícitos,
e cada capacidade será acrescentada sobre o catálogo validado sem colocar lógica Tractian no
núcleo.
