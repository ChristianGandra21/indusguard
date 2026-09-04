# Dashboard e playground web

Frontend estático do IndusGuard. As páginas de dashboard continuam somente leitura. A rota
`/playground` é exclusiva do proprietário, renderiza os conectores publicados pelo backend e
mantém o token Bearer somente no `sessionStorage` da aba.

```bash
npm install
cp .env.example .env.local
npm run dev
```

A API deve estar em `http://127.0.0.1:8000`. Para outro endereço, altere
`NEXT_PUBLIC_INDUSGUARD_API_URL` antes do build: a exportação estática incorpora esse valor no
JavaScript entregue ao navegador.

Comandos principais:

- `npm run dev`: desenvolvimento em `http://localhost:3000`;
- `npm run test`: testes de componentes e contratos;
- `npm run typecheck`: tipagem TypeScript;
- `npm run build`: exportação estática em `out/`;
- `npm run api:generate`: regenera tipos a partir do snapshot OpenAPI versionado;
- `npm run test:e2e`: smoke Playwright contra servidores já iniciados.

O dashboard mostra honestamente quando ainda não existe avaliação. `offline_smoke` é sempre
rotulado como teste de infraestrutura; `groq_pilot` é real, mas experimental; somente
`groq_benchmark` pode aparecer como evidência científica.

A rota `/trace` consulta `GET /api/v1/runs/recent` para oferecer um dropdown de runs recentes e
continua aceitando um `run_id` colado manualmente. A lista carrega somente metadados seguros:
conector, estado, decisão, intenção, modelo e timestamps.

## Playground protegido

O navegador primeiro consulta `GET /api/v1/playground/config`. Depois que a pessoa informa o
token, ele é salvo em `sessionStorage` sob `indusguard.owner_token` e enviado somente no header
`Authorization` do `POST /api/v1/runs`. Ele não entra em URL, query key, logs ou build.

A resposta fica apenas no estado da página e apresenta:

- resposta fundamentada e incertezas;
- link direto para o trace público da run;
- evidências redigidas;
- timeline de tools e decisions da policy;
- tokens, latência, término e truncamentos;
- estados específicos para cold start, 401, quota, concorrência, modelo ausente e run parcial.

O formulário monta campos de contexto a partir de `context_fields`, omite campos controlados pelo
servidor, como `user_id`, e usa listas de seleção para os identificadores conhecidos. Escolher um
caso Tractian preenche empresa e ativo relacionados, evitando que a pessoa precise decorar IDs.
Quando o backend publica o conector Tractian no playground, a tela expõe roteiros industriais para
investigação sem aviso e pedido de especialista sem versionar payloads de eval ou abrir goldens.

O E2E injeta um modelo fake somente pelo seam da app factory. FastAPI, host público, LangGraph,
MCP, policy e upstream synthetic continuam reais e offline.
