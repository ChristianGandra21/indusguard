# Guia de conectores

Um conector adapta uma API REST JSON ao núcleo do IndusGuard sem exigir código Python específico.
Ele não é apenas uma cópia do OpenAPI: também registra política e linguagem de domínio.

## Estrutura esperada

```text
connectors/
└── minha_api/
    ├── openapi.yaml   # contrato técnico OpenAPI 3.x
    ├── profile.yaml   # conexão, autenticação e políticas
    └── domain.yaml    # vocabulário, intenções e contexto
```

O nome da pasta e o campo `id` do profile precisam ser iguais. Por exemplo, a pasta `minha_api/`
precisa conter `id: minha_api`.

O loader tolera a ausência de `domain.yaml` para contratos estritamente técnicos, mas a convenção
do projeto exige os três arquivos para que a integração também seja compreensível pelo agente e
pela interface.

## Como os arquivos se complementam

### `openapi.yaml`

É a fonte técnica para paths, métodos, parâmetros, bodies e responses. Cada operação precisa de um
`operationId` único, pois esse identificador conecta o contrato às políticas e ao domínio.

Limites atuais:

- OpenAPI 3.0 ou 3.1;
- REST com JSON;
- `$ref` somente local, começando com `#/`;
- sem upload ou formato `binary`/`byte`;
- sem GraphQL, gRPC, WebSocket ou OAuth interativo.

### `profile.yaml`

É a decisão local de confiança. O exemplo abaixo é pequeno, mas completo:

```yaml
id: inventory
name: Inventory API
description: Consulta e atualiza itens de estoque.
openapi: ./openapi.yaml

# O valor é lido de INVENTORY_API_URL pelo executor.
base_url_env: INVENTORY_API_URL
allowed_base_urls:
  - https://inventory.example.com

auth:
  type: api_key_header
  name: x-api-key
  env: INVENTORY_API_KEY

operations:
  getItem:
    enabled: true
    access: read
    risk: low
    timeout_seconds: 5
    max_retries: 2
    idempotent: true

  updateItem:
    enabled: true
    access: write
    risk: high
    permission: inventory_write
    requires_direct_request: true
    requires_confirmation: true
    justification_min_length: 20
    timeout_seconds: 5
    redact_fields: [internal_note]
```

#### Autenticação

| `type` | Campos adicionais | Origem do valor | Runtime atual |
|---|---|---|---|
| `none` | nenhum | Não há autenticação. | Implementado. |
| `api_key_header` | `name`, `env` | Variável indicada em `env`, enviada no header. | Bloqueado. |
| `api_key_query` | `name`, `env` | Variável indicada em `env`, enviada na query. | Bloqueado. |
| `bearer` | `env` | Variável indicada em `env`, enviada como Bearer token. | Bloqueado. |
| `context_header` | `name`, `context_field` | Campo do contexto validado da execução. | Implementado. |

O profile guarda somente nomes de variáveis, nunca os segredos. O catálogo público também não
resolve nem devolve esses valores.

#### Política por operação

| Campo | Default | Por que existe |
|---|---:|---|
| `enabled` | `false` | Exige liberação consciente de cada endpoint. |
| `access` | derivado do HTTP | Impede confundir leitura e escrita. |
| `risk` | `low` para leitura, `high` para escrita | Orienta confirmação e release gates. |
| `permission` | vazio | Permissão de domínio necessária para executar. |
| `requires_direct_request` | `false` | Impede o agente de inferir uma ação não solicitada. |
| `requires_confirmation` | `false` | Adiciona confirmação antes de operações sensíveis. |
| `justification_min_length` | `0` | Exige justificativa minimamente informativa. |
| `timeout_seconds` | `10` | Limita quanto uma tool pode bloquear a execução. |
| `max_retries` | `0` | Limita repetição automática; o máximo aceito é 2. |
| `idempotent` | `false` | Informa se repetir a chamada preserva o efeito. |
| `redact_fields` | `[]` | Define campos que não devem aparecer em traces. |

O executor já aplica `enabled`, timeout e allowlist em operações GET. Política de permissão,
confirmação, retry, idempotência e redaction será conectada nos próximos incrementos.

### `domain.yaml`

Registra conhecimento de integração que não pertence ao Python:

```yaml
id: inventory
language: pt-BR
context_fields: [user_id, warehouse_id, item_id]

terminology:
  item: produto armazenado e identificado por SKU

intents:
  - id: consultar
    description: Consultar saldo e localização de um item.
    evidence_operations: [getItem]
  - id: atualizar
    description: Solicitar uma alteração autorizada no item.
    action_operations: [updateItem]
```

No estágio atual, o loader valida `context_fields` e exige que um `context_header` aponte para um
campo declarado nessa lista. Terminologia e intenções serão consumidas pelo classificador e pelo
agente em etapas futuras.

## Adicionar uma API passo a passo

1. Crie `connectors/<id>/`.
2. Coloque um OpenAPI 3.x em `openapi.yaml`.
3. Garanta um `operationId` único em cada endpoint.
4. Escreva `profile.yaml` e habilite somente as operações necessárias.
5. Declare autenticação sem copiar credenciais para o arquivo.
6. Escreva `domain.yaml` com contexto e vocabulário.
7. Execute `make validate`.
8. Execute `make test` para verificar que os conectores anteriores continuam válidos.
9. Confira o resultado em `/api/v1/connectors/<id>/operations`.

Se qualquer conector estiver inválido, o serviço falha no startup. Isso é intencional: uma
integração parcialmente carregada não deve ser anunciada como pronta.

## Conectores incluídos

### `tractian`

Primeiro caso real do projeto. Possui 18 operações industriais e autenticação derivada do campo
`user_id`. As leituras reutilizam uma âncora YAML; escritas têm políticas individuais.

O contrato recebido repetia `/assets/{assetId}` para GET e PATCH. A cópia versionada une os dois
métodos sob uma única chave e há um teste impedindo regressão.

### `synthetic`

API mínima com `getWidget` e `updateWidget`. Seu objetivo é provar que adicionar outro domínio não
exige editar FastAPI, Pydantic ou a futura UI. `getWidget` também possui query array `labels` e o
header opcional `x-request-id`, usados para testar serialização genérica.

## Erros frequentes

| Mensagem | Causa provável |
|---|---|
| `chave YAML duplicada` | O mesmo path ou campo aparece duas vezes. Una os conteúdos. |
| `operationId duplicado` | Duas operações usam o mesmo identificador. |
| `políticas apontam para operationIds inexistentes` | Há typo ou drift entre profile e OpenAPI. |
| `o arquivo OpenAPI deve permanecer dentro do conector` | O profile tentou usar `../`. |
| `$ref externo não é permitido` | O schema depende de outro arquivo ou URL. Internalize o ref. |
| `usa conteúdo não JSON` | A operação usa upload, stream ou mídia fora do escopo da v1. |
