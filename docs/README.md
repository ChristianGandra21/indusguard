# Índice da documentação

Esta pasta aprofunda decisões que deixariam o README principal longo demais.

| Documento | Quando consultar |
|---|---|
| [Guia completo](./GUIA_COMPLETO.md) | Para estudar o projeto inteiro, do conceito ao código. |
| [Aula do executor](./executor-study-guide.md) | Para aprender HTTP do zero e acompanhar `getAsset` até o request final. |
| [Arquitetura](./architecture.md) | Para entender componentes, fronteiras e fluxo de startup. |
| [Guia do código](./code-guide.md) | Para estudar os arquivos Python na ordem recomendada. |
| [Material dos stakeholders](./stakeholder-material.md) | Para conhecer a fixture Tractian, seus testes e limitações. |
| [Guia de conectores](../connectors/README.md) | Para entender ou adicionar uma integração OpenAPI. |
| [Benchmark e avaliações](../evals/README.md) | Para entender corpus, baseline, scorer, resume e ressalvas. |
| [Dashboard web](../apps/web/README.md) | Para executar o Next.js estático e entender seus limites públicos. |

## Regra de atualização

Uma funcionalidade só deve aparecer como concluída quando existir código e teste correspondente.
Ideias futuras ficam identificadas como roadmap. Isso evita que a documentação prometa execução
pública do agente, escrita real ou deployment que ainda não existem no repositório.
