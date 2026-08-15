# Conectores

Cada subdiretório representa uma API REST JSON e contém:

- `openapi.yaml`: contrato OpenAPI 3.x, sem `$ref` externo ou payload binário;
- `profile.yaml`: autenticação, allowlist de URL e política explícita por `operationId`;
- `domain.yaml`: vocabulário, intenções e campos de contexto do domínio.

Operações que existem no OpenAPI, mas não no perfil, são carregadas desabilitadas. Políticas que
apontam para um `operationId` inexistente invalidam o conector. Credenciais são referenciadas pelo
nome da variável de ambiente e nunca são armazenadas nestes arquivos.

O conector `synthetic` funciona como teste de arquitetura: ele é descoberto sem importação ou
código específico no backend.
