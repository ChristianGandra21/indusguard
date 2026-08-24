# Dashboard web

Frontend estático e somente leitura do IndusGuard. Ele consome as projeções públicas do FastAPI e
nunca recebe mensagens, argumentos de tools, respostas industriais, contexto confiável ou golden
set.

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
rotulado como teste de infraestrutura sem valor científico.
