# Guia de leitura do código

Este documento sugere uma ordem para estudar o backend sem precisar entender tudo de uma vez.

## 1. `settings.py`: de onde vêm as configurações

Comece em [`settings.py`](../apps/api/src/indusguard_api/settings.py). A classe `Settings` usa o
prefixo `INDUSGUARD_`, portanto o campo `execution_mode` é configurado por
`INDUSGUARD_EXECUTION_MODE`.

O default é `simulate`. Mesmo antes do executor existir, registrar esse default no contrato evita
que uma etapa futura introduza escrita real por acidente.

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

As rotas atuais não executam a API Tractian; apenas expõem metadados já validados.

## 5. Testes: qual comportamento não pode regredir

Leia [`test_connectors.py`](../apps/api/tests/test_connectors.py) depois do núcleo. Os testes provam
cinco propriedades arquiteturais:

1. dois conectores são descobertos sem registro manual;
2. GET e PATCH do mesmo path Tractian continuam presentes;
3. conector desconhecido retorna 404;
4. operação sem profile fica desabilitada;
5. YAML duplicado é rejeitado.

[`test_system.py`](../apps/api/tests/test_system.py) cobre liveness, readiness e o default seguro de
simulação.

## 6. O que ainda não procurar no código

Ainda não existem executor HTTP, chamada de LLM, MCP, LangGraph, banco ou frontend. Esses nomes
aparecem no roadmap, mas não no caminho de execução atual. A próxima camada será construída sobre
o catálogo validado, sem colocar lógica Tractian dentro do executor.
