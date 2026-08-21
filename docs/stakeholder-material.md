# Avaliação do material dos stakeholders

## Escopo recebido

O pacote Tractian × Inteli fornece:

- uma API FastAPI com 18 operações;
- 13 arquivos de dados sintéticos, incluindo 8 empresas, 26 ativos e 24 análises;
- 17 chamados de entrada organizados em 16 cenários;
- comportamento `complete`, `partial`, `inconclusive`, `conflict` e `unavailable`;
- 39 testes da API;
- trajetórias esperadas para avaliação.

O pacote é uma boa fixture educacional. Ele não é tratado como referência de segurança ou
deployment de produção.

## Verificações realizadas

- os 39 testes fornecidos passaram;
- as 57 chamadas listadas nas trajetórias esperadas responderam com sucesso;
- não foram encontrados registros duplicados nos Parquets;
- as relações principais entre empresas, ativos, análises e pontos são válidas;
- foi encontrada uma inconsistência entre a empresa de `usr_carla` e o caso `TKT-EXE-15`.

## Limitações relevantes

### Path duplicado no OpenAPI

O contrato original repetia `/assets/{assetId}`: primeiro GET, depois PATCH. Parsers YAML comuns
podem manter somente a última chave. A cópia em `connectors/tractian/openapi.yaml` normaliza os dois
métodos sob um único path. Nenhuma outra alteração foi feita no contrato.

### Escritas permissivas

A fixture valida permissão e justificativa, mas aceita alguns payloads fora do domínio, como uma
criticidade não prevista no enum. Por isso, o executor valida argumentos pelo OpenAPI antes de
fazer a chamada; um teste de body protege esse comportamento.

### Sem isolamento por empresa

A autorização da fixture é baseada em permissões do usuário, não na relação usuário–empresa–ativo.
A policy engine do IndusGuard agora verifica identidade, empresa e escopo do recurso antes de
aprovar uma simulação. Escrita real continua desabilitada.

### Gabarito distribuído em vários arquivos

As respostas esperadas aparecem em `eval/`, `docs/test-scenarios.md`, `data/cases.parquet` e no
gerador de dados. Esses artefatos não poderão entrar na imagem de runtime ou no contexto do agente.

### Indisponibilidade não é erro HTTP

O modo `unavailable` chega em um envelope HTTP 200. Timeout, 429 e 5xx terão que ser adicionados por
mocks ou pelo conector sintético para testar retry e circuit breaker.

### Ações não persistem

Na fixture, `accepted=true` encerra a ação com sucesso. Consultar o recurso novamente não garante
que o novo estado apareça. A UI e o relatório devem deixar essa semântica explícita.

## Decisões aplicadas no IndusGuard

1. contrato normalizado e protegido por teste de regressão;
2. YAML duplicado rejeitado no startup;
3. toda operação precisa de política explícita para ser habilitada;
4. mutações começam em `simulate`;
5. gabarito permanece separado do runtime;
6. segundo conector sintético prova que a arquitetura não depende da Tractian.
